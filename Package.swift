// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "handsfree-audio-helper",
    platforms: [.macOS(.v13)],
    products: [
        .executable(
            name: "handsfree-audio-helper",
            targets: ["handsfree-audio-helper"]
        )
    ],
    targets: [
        .executableTarget(name: "handsfree-audio-helper")
    ],
    swiftLanguageModes: [.v5]
)
