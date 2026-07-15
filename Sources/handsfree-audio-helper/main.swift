@preconcurrency import AVFoundation
import Darwin
import Foundation

private enum Command: UInt8 {
    case playPCM = 0x01
    case stop = 0x02
    case shutdown = 0x03
}

private enum OutputMessage: UInt8 {
    case micMetadata = 0x81
    case micPCM = 0x82
    case event = 0x83
}

private enum HelperError: Error, CustomStringConvertible {
    case invalidCommand(UInt8)
    case invalidPCM(String)
    case probeMicTimeout
    case truncatedInput

    var description: String {
        switch self {
        case .invalidCommand(let byte):
            return "unknown command byte \(byte)"
        case .invalidPCM(let reason):
            return "invalid PCM payload: \(reason)"
        case .probeMicTimeout:
            return "microphone did not deliver a frame before the probe deadline"
        case .truncatedInput:
            return "stdin closed in the middle of a frame"
        }
    }
}

private func writeProbeLine(_ line: String) {
    fputs("\(line)\n", stdout)
    fflush(stdout)
}

private final class ProbeState: @unchecked Sendable {
    private let lock = NSLock()
    private let micFrameSemaphore = DispatchSemaphore(value: 0)
    private var stalledAt = "starting probe"
    private var engineHasStarted = false
    private var micFramePending = false
    private var receivedFirstMicFrame = false

    func begin(_ step: String) {
        lock.lock()
        stalledAt = step
        lock.unlock()
    }

    func completed(_ line: String) {
        lock.lock()
        stalledAt = "after \(line)"
        lock.unlock()
        writeProbeLine(line)
    }

    func micFrameReceived() {
        lock.lock()
        guard !receivedFirstMicFrame, !micFramePending else {
            lock.unlock()
            return
        }
        guard engineHasStarted else {
            micFramePending = true
            lock.unlock()
            return
        }
        receivedFirstMicFrame = true
        stalledAt = "after mic frame received"
        lock.unlock()
        writeProbeLine("mic frame received")
        micFrameSemaphore.signal()
    }

    func engineStarted() {
        lock.lock()
        engineHasStarted = true
        stalledAt = "after engine started"
        let releasePendingMicFrame = micFramePending
        if releasePendingMicFrame {
            micFramePending = false
            receivedFirstMicFrame = true
            stalledAt = "after mic frame received"
        }
        lock.unlock()

        writeProbeLine("engine started")
        if releasePendingMicFrame {
            writeProbeLine("mic frame received")
            micFrameSemaphore.signal()
        }
    }

    func waitForMicFrame(until deadline: DispatchTime) -> Bool {
        micFrameSemaphore.wait(timeout: deadline) == .success
    }

    func timeoutLocation() -> String {
        lock.lock()
        defer { lock.unlock() }
        return stalledAt
    }
}

private final class FrameWriter: @unchecked Sendable {
    enum Mode { case framed, diagnostic }

    private let mode: Mode
    private let queue = DispatchQueue(label: "handsfree.audio-helper.stdout")

    init(mode: Mode) {
        self.mode = mode
    }

    func send(_ type: OutputMessage, payload: Data) {
        guard mode == .framed else { return }
        queue.async {
            var header = Data([type.rawValue])
            var length = UInt32(payload.count).littleEndian
            withUnsafeBytes(of: &length) { header.append(contentsOf: $0) }
            FileHandle.standardOutput.write(header)
            FileHandle.standardOutput.write(payload)
        }
    }

    func event(_ name: String, details: [String: Any] = [:]) {
        var body = details
        body["event"] = name
        body["timestamp_ns"] = DispatchTime.now().uptimeNanoseconds
        guard let data = try? JSONSerialization.data(withJSONObject: body) else { return }
        if mode == .diagnostic {
            FileHandle.standardError.write(data)
            FileHandle.standardError.write(Data("\n".utf8))
        } else {
            send(.event, payload: data)
        }
    }

    func flush() {
        queue.sync {}
    }
}

private final class VoiceProcessingEngine: @unchecked Sendable {
    private let engine: AVAudioEngine
    private let player: AVAudioPlayerNode
    private let writer: FrameWriter
    private let queue = DispatchQueue(label: "handsfree.audio-helper.engine")
    private let activeLock = NSLock()
    private var configurationObserver: NSObjectProtocol?
    private var active = false
    private var running = false
    private var tapInstalled = false
    private var generation: UInt64 = 0

    init(
        writer: FrameWriter,
        engine: AVAudioEngine = AVAudioEngine(),
        player: AVAudioPlayerNode = AVAudioPlayerNode()
    ) {
        self.writer = writer
        self.engine = engine
        self.player = player
        engine.attach(player)
        configurationObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange,
            object: engine,
            queue: nil
        ) { [weak self] _ in
            self?.audioEngineConfigurationChanged()
        }
    }

    deinit {
        if let configurationObserver {
            NotificationCenter.default.removeObserver(configurationObserver)
        }
    }

    private func setActive(_ value: Bool) {
        activeLock.lock()
        active = value
        activeLock.unlock()
    }

    private func isActive() -> Bool {
        activeLock.lock()
        defer { activeLock.unlock() }
        return active
    }

    private func audioEngineConfigurationChanged() {
        // Enabling voice processing can itself reconfigure the graph. Only a
        // notification observed after start represents an active route change.
        guard isActive() else { return }
        queue.async { [self] in
            guard running else { return }
            stopLocked(emitEvent: false)
            writer.event("helper_unavailable", details: [
                "message": "audio input or output device configuration changed",
            ])
        }
    }

    private func enableVoiceProcessing(progress: ProbeState? = nil) throws {
        progress?.begin("enabling voice processing input")
        let input = engine.inputNode
        try input.setVoiceProcessingEnabled(true)
        progress?.completed("voice processing input enabled")

        progress?.begin("enabling voice processing output")
        let output = engine.outputNode
        try output.setVoiceProcessingEnabled(true)
        progress?.completed("voice processing output enabled")

        guard input.isVoiceProcessingEnabled, output.isVoiceProcessingEnabled else {
            throw HelperError.invalidPCM("AVAudioEngine did not enable voice processing")
        }
    }

    private func installMicTap(onMicFrame: (@Sendable () -> Void)? = nil) throws {
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            throw HelperError.invalidPCM("audio input device has no active format")
        }
        let metadata: [String: Any] = [
            "sample_rate": format.sampleRate,
            "channels": format.channelCount,
            "voice_processing_input": input.isVoiceProcessingEnabled,
            "voice_processing_output": engine.outputNode.isVoiceProcessingEnabled,
        ]
        if let data = try? JSONSerialization.data(withJSONObject: metadata) {
            writer.send(.micMetadata, payload: data)
        }

        input.installTap(onBus: 0, bufferSize: 1440, format: format) { [writer] buffer, _ in
            guard let channels = buffer.floatChannelData, buffer.frameLength > 0 else { return }
            onMicFrame?()
            let frameCount = Int(buffer.frameLength)
            let channelCount = max(1, Int(format.channelCount))
            var pcm = Data(count: frameCount * MemoryLayout<Int16>.size)
            pcm.withUnsafeMutableBytes { rawBuffer in
                let output = rawBuffer.bindMemory(to: Int16.self)
                for frame in 0..<frameCount {
                    var sample: Float = 0
                    for channel in 0..<channelCount {
                        sample += channels[channel][frame]
                    }
                    sample /= Float(channelCount)
                    let clipped = max(-1, min(1, sample))
                    output[frame] = Int16(clipped * Float(Int16.max))
                }
            }
            writer.send(.micPCM, payload: pcm)
        }
        tapInstalled = true
    }

    private func makeBuffer(from payload: Data) throws -> (AVAudioPCMBuffer, AVAudioFormat) {
        guard payload.count >= 12 else {
            throw HelperError.invalidPCM("missing 12-byte metadata header")
        }
        let sampleRate = payload.readUInt32LE(at: 0)
        let channelCount = payload.readUInt32LE(at: 4)
        let sampleWidth = payload.readUInt32LE(at: 8)
        guard sampleRate > 0, channelCount == 1, sampleWidth == 2 else {
            throw HelperError.invalidPCM("expected mono int16 PCM")
        }
        let audio = payload.dropFirst(12)
        guard audio.count % 2 == 0 else {
            throw HelperError.invalidPCM("odd byte count")
        }
        let frameCount = AVAudioFrameCount(audio.count / 2)
        guard let format = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: Double(sampleRate),
            channels: 1,
            interleaved: false
        ), let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frameCount),
              let output = buffer.floatChannelData?[0] else {
            throw HelperError.invalidPCM("could not allocate AVAudioPCMBuffer")
        }
        buffer.frameLength = frameCount
        audio.withUnsafeBytes { rawBuffer in
            guard let baseAddress = rawBuffer.baseAddress else { return }
            let source = baseAddress.assumingMemoryBound(to: Int16.self)
            for index in 0..<Int(frameCount) {
                output[index] = Float(source[index]) / 32768.0
            }
        }
        return (buffer, format)
    }

    func playPCM(_ payload: Data) {
        queue.async { [self] in
            do {
                stopLocked(emitEvent: false)
                let (buffer, format) = try makeBuffer(from: payload)
                generation &+= 1
                let playbackGeneration = generation

                try enableVoiceProcessing()
                engine.connect(player, to: engine.mainMixerNode, format: format)
                try installMicTap()
                engine.prepare()
                try engine.start()
                running = true
                setActive(true)

                player.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { [weak self] _ in
                    self?.queue.async {
                        guard let self, self.running, self.generation == playbackGeneration else { return }
                        self.stopLocked(emitEvent: false)
                        self.writer.event("playback_completed")
                    }
                }
                player.play()
                writer.event("playback_started", details: [
                    "voice_processing_input": engine.inputNode.isVoiceProcessingEnabled,
                    "voice_processing_output": engine.outputNode.isVoiceProcessingEnabled,
                ])
            } catch {
                writer.event("error", details: ["message": String(describing: error)])
                stopLocked(emitEvent: false)
            }
        }
    }

    func stopPlayback() {
        queue.async { [self] in
            guard running else { return }
            stopLocked(emitEvent: false)
            writer.event("playback_stopped")
        }
    }

    private func stopLocked(emitEvent: Bool) {
        setActive(false)
        generation &+= 1
        player.stop()
        if tapInstalled {
            engine.inputNode.removeTap(onBus: 0)
            tapInstalled = false
        }
        if running {
            engine.stop()
            engine.reset()
        }
        running = false
        if emitEvent { writer.event("engine_stopped") }
    }

    func shutdown(completion: @escaping @Sendable () -> Void) {
        queue.async { [self] in
            stopLocked(emitEvent: true)
            writer.flush()
            completion()
        }
    }

    func playWAV(at url: URL) throws {
        let file = try AVAudioFile(forReading: url)
        let format = file.processingFormat
        guard let buffer = AVAudioPCMBuffer(
            pcmFormat: format,
            frameCapacity: AVAudioFrameCount(file.length)
        ) else {
            throw HelperError.invalidPCM("could not allocate WAV buffer")
        }
        try file.read(into: buffer)
        try enableVoiceProcessing()
        engine.connect(player, to: engine.mainMixerNode, format: format)
        try installMicTap()
        engine.prepare()
        try engine.start()
        running = true

        let done = DispatchSemaphore(value: 0)
        player.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { _ in done.signal() }
        player.play()
        writer.event("playback_started", details: [
            "voice_processing_input": engine.inputNode.isVoiceProcessingEnabled,
            "voice_processing_output": engine.outputNode.isVoiceProcessingEnabled,
        ])
        done.wait()
        writer.event("playback_completed")
        stopLocked(emitEvent: false)
        writer.flush()
    }
}

private extension Data {
    func readUInt32LE(at offset: Int) -> UInt32 {
        let bytes = self[offset..<(offset + 4)]
        return bytes.enumerated().reduce(UInt32(0)) { partial, pair in
            partial | (UInt32(pair.element) << UInt32(pair.offset * 8))
        }
    }
}

private func readExact(_ count: Int, from handle: FileHandle) throws -> Data? {
    var data = Data()
    while data.count < count {
        guard let chunk = try handle.read(upToCount: count - data.count), !chunk.isEmpty else {
            if data.isEmpty { return nil }
            throw HelperError.truncatedInput
        }
        data.append(chunk)
    }
    return data
}

private func runStdio() -> Never {
    let writer = FrameWriter(mode: .framed)
    let controller = VoiceProcessingEngine(writer: writer)

    signal(SIGTERM, SIG_IGN)
    signal(SIGINT, SIG_IGN)
    let termSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .global())
    let intSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: .global())
    let terminate: @Sendable () -> Void = {
        controller.shutdown { exit(EXIT_SUCCESS) }
    }
    termSource.setEventHandler(handler: terminate)
    intSource.setEventHandler(handler: terminate)
    termSource.resume()
    intSource.resume()

    DispatchQueue.global(qos: .userInitiated).async {
        do {
            let input = FileHandle.standardInput
            while let header = try readExact(5, from: input) {
                guard let command = Command(rawValue: header[0]) else {
                    throw HelperError.invalidCommand(header[0])
                }
                let length = Int(header.readUInt32LE(at: 1))
                guard let payload = try readExact(length, from: input) else {
                    throw HelperError.truncatedInput
                }
                switch command {
                case .playPCM: controller.playPCM(payload)
                case .stop: controller.stopPlayback()
                case .shutdown: terminate()
                }
            }
            terminate()
        } catch {
            writer.event("error", details: ["message": String(describing: error)])
            controller.shutdown { exit(EXIT_FAILURE) }
        }
    }
    dispatchMain()
}

private func diagnosticWriter() -> FrameWriter {
    FrameWriter(mode: .diagnostic)
}

private func executeProbe(progress: ProbeState) throws -> [String: Any] {
    progress.begin("creating AVAudioEngine")
    let engine = AVAudioEngine()
    progress.completed("engine created")

    var tapInstalled = false
    var engineStarted = false
    defer {
        if tapInstalled {
            engine.inputNode.removeTap(onBus: 0)
        }
        if engineStarted {
            engine.stop()
            engine.reset()
        }
    }

    // The probe only validates duplex I/O and does not play audio. Avoid
    // constructing AVAudioPlayerNode here: it performs an unrelated AudioUnit
    // component lookup and previously blocked/crashed before mic diagnostics.
    progress.begin("enabling voice processing input")
    let input = engine.inputNode
    try input.setVoiceProcessingEnabled(true)
    progress.completed("voice processing input enabled")

    progress.begin("enabling voice processing output")
    let output = engine.outputNode
    try output.setVoiceProcessingEnabled(true)
    progress.completed("voice processing output enabled")

    guard input.isVoiceProcessingEnabled, output.isVoiceProcessingEnabled else {
        throw HelperError.invalidPCM("AVAudioEngine did not enable voice processing")
    }

    progress.begin("querying the default input device format")
    let format = input.outputFormat(forBus: 0)
    guard format.sampleRate > 0, format.channelCount > 0 else {
        throw HelperError.invalidPCM("audio input device has no active format")
    }

    progress.begin("installing the microphone tap")
    input.installTap(onBus: 0, bufferSize: 1440, format: format) { buffer, _ in
        guard buffer.frameLength > 0 else { return }
        progress.micFrameReceived()
    }
    tapInstalled = true

    progress.begin("preparing the audio engine")
    engine.prepare()
    progress.begin("starting the audio engine")
    try engine.start()
    engineStarted = true
    progress.engineStarted()

    progress.begin("waiting for a microphone frame")
    guard progress.waitForMicFrame(until: .now() + .seconds(9)) else {
        throw HelperError.probeMicTimeout
    }

    return [
        "voice_processing_input": input.isVoiceProcessingEnabled,
        "voice_processing_output": output.isVoiceProcessingEnabled,
        "input_sample_rate": format.sampleRate,
        "input_channels": format.channelCount,
    ]
}

private func runProbe() -> Never {
    // stdout is commonly a pipe when the probe is launched by an installer or
    // test harness. Disable buffering and still flush every progress line so a
    // blocked CoreAudio call cannot hide the last completed step.
    setbuf(stdout, nil)
    setbuf(stderr, nil)

    let progress = ProbeState()
    DispatchQueue.global(qos: .userInitiated).asyncAfter(deadline: .now() + .seconds(10)) {
        fputs("probe timed out after 10s; stalled at: \(progress.timeoutLocation())\n", stderr)
        fflush(stderr)
        _exit(EXIT_FAILURE)
    }

    do {
        let result = try executeProbe(progress: progress)
        let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
        writeProbeLine(String(decoding: data, as: UTF8.self))
        exit(EXIT_SUCCESS)
    } catch {
        fputs("probe failed at \(progress.timeoutLocation()): \(error)\n", stderr)
        fflush(stderr)
        exit(EXIT_FAILURE)
    }
}

switch CommandLine.arguments.dropFirst().first {
case "--probe":
    runProbe()
case "--play-wav":
    guard CommandLine.arguments.count == 3 else {
        fputs("usage: handsfree-audio-helper --play-wav PATH\n", stderr)
        exit(EXIT_FAILURE)
    }
    do {
        try VoiceProcessingEngine(writer: diagnosticWriter()).playWAV(
            at: URL(fileURLWithPath: CommandLine.arguments[2])
        )
        exit(EXIT_SUCCESS)
    } catch {
        fputs("playback failed: \(error)\n", stderr)
        exit(EXIT_FAILURE)
    }
case nil, "--stdio":
    runStdio()
default:
    fputs("usage: handsfree-audio-helper [--stdio|--probe|--play-wav PATH]\n", stderr)
    exit(EXIT_FAILURE)
}
