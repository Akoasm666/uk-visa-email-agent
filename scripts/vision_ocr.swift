import CoreGraphics
import Foundation
import ImageIO
import Vision

private let maxEdge = 1800
private let maxPages = 12
private let maxInputPixels = 32_000_000

private enum ScanError: Error {
    case invalidDocument
    case inputLimit
    case lockedPDF
}

private struct Page: Encodable {
    let text: String
    let sharpness: Double?
    let touchesEdge: Bool
}

private func pdfImage(_ page: CGPDFPage) throws -> CGImage {
    let box = page.getBoxRect(.cropBox)
    let rotated = page.rotationAngle % 180 != 0
    let width = rotated ? box.height : box.width
    let height = rotated ? box.width : box.height
    guard width.isFinite, height.isFinite, width > 0, height > 0 else {
        throw ScanError.invalidDocument
    }
    let scale = CGFloat(maxEdge) / max(width, height)
    guard let canvas = CGContext(
        data: nil, width: max(1, Int(width * scale)), height: max(1, Int(height * scale)),
        bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ) else { throw ScanError.invalidDocument }
    let target = CGRect(x: 0, y: 0, width: canvas.width, height: canvas.height)
    canvas.setFillColor(gray: 1, alpha: 1)
    canvas.fill(target)
    canvas.concatenate(page.getDrawingTransform(.cropBox, rect: target, rotate: 0,
                                                preserveAspectRatio: true))
    canvas.drawPDFPage(page)
    guard let image = canvas.makeImage() else { throw ScanError.invalidDocument }
    return image
}

private func rasterImage(_ url: URL) throws -> CGImage {
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
          CGImageSourceGetCount(source) == 1,
          let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
          let width = properties[kCGImagePropertyPixelWidth] as? Int,
          let height = properties[kCGImagePropertyPixelHeight] as? Int,
          width > 0, height > 0 else { throw ScanError.invalidDocument }
    guard width <= maxInputPixels / height else { throw ScanError.inputLimit }
    let options: [CFString: Any] = [
        kCGImageSourceCreateThumbnailFromImageAlways: true,
        kCGImageSourceCreateThumbnailWithTransform: true,
        kCGImageSourceThumbnailMaxPixelSize: maxEdge
    ]
    guard let image = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary),
          CGImageSourceGetStatusAtIndex(source, 0) == .statusComplete else {
        throw ScanError.invalidDocument
    }
    return image
}

// Ratio of Laplacian energy to gradient energy around recognised text. Relative
// energy limits the effect of low contrast and excludes backgrounds/photos.
private func textSharpness(_ image: CGImage, boxes: [CGRect]) -> Double? {
    let width = image.width
    let height = image.height
    guard width >= 3, height >= 3, !boxes.isEmpty,
          let gray = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8,
                               bytesPerRow: width, space: CGColorSpaceCreateDeviceGray(),
                               bitmapInfo: CGImageAlphaInfo.none.rawValue) else { return nil }
    gray.setFillColor(gray: 1, alpha: 1)
    let target = CGRect(x: 0, y: 0, width: width, height: height)
    gray.fill(target)
    gray.draw(image, in: target)
    guard let bytes = gray.data?.assumingMemoryBound(to: UInt8.self) else { return nil }
    let stride = gray.bytesPerRow
    let scores = boxes.compactMap { box -> Double? in
        let left = max(1, Int(box.minX * CGFloat(width)))
        let right = min(width - 1, Int(ceil(box.maxX * CGFloat(width))))
        let top = max(1, Int((1 - box.maxY) * CGFloat(height)))
        let bottom = min(height - 1, Int(ceil((1 - box.minY) * CGFloat(height))))
        guard left < right, top < bottom else { return nil }
        var laplacian = 0.0
        var gradient = 0.0
        for y in top..<bottom {
            for x in left..<right {
                let offset = y * stride + x
                let horizontal = Double(bytes[offset + 1]) - Double(bytes[offset - 1])
                let vertical = Double(bytes[offset + stride]) - Double(bytes[offset - stride])
                let second = Double(bytes[offset + 1]) + Double(bytes[offset - 1])
                    + Double(bytes[offset + stride]) + Double(bytes[offset - stride])
                    - 4 * Double(bytes[offset])
                laplacian += second * second
                gradient += horizontal * horizontal + vertical * vertical
            }
        }
        return gradient > 0 ? laplacian / gradient : nil
    }.sorted()
    return scores.isEmpty ? nil : scores[scores.count / 2]
}

private func readPage(_ image: CGImage) throws -> Page {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    if #available(macOS 12.0, *) {
        let supported = Set(try request.supportedRecognitionLanguages())
        let languages = ["zh-Hans", "zh-Hant", "en-US"].filter { supported.contains($0) }
        if !languages.isEmpty {
            request.recognitionLanguages = languages
        }
    }
    request.usesLanguageCorrection = false
    request.usesCPUOnly = true
    request.minimumTextHeight = 0.006
    try VNImageRequestHandler(cgImage: image).perform([request])
    let observations = (request.results ?? []).prefix(300)
    let lines = observations.compactMap { $0.topCandidates(1).first?.string }
    let boxes = observations.map(\.boundingBox)
    let touchesEdge = boxes.contains {
        $0.minX < 0.012 || $0.maxX > 0.988 || $0.minY < 0.012 || $0.maxY > 0.988
    }
    return Page(text: lines.joined(separator: "\n"), sharpness: textSharpness(image, boxes: boxes),
                touchesEdge: touchesEdge)
}

do {
    guard CommandLine.arguments.count == 3 else { throw ScanError.invalidDocument }
    let url = URL(fileURLWithPath: CommandLine.arguments[2])
    var pages: [Page] = []
    if CommandLine.arguments[1] == "pdf" {
        guard let document = CGPDFDocument(url as CFURL) else { throw ScanError.invalidDocument }
        guard document.isUnlocked else { throw ScanError.lockedPDF }
        guard (1...maxPages).contains(document.numberOfPages) else { throw ScanError.inputLimit }
        for number in 1...document.numberOfPages {
            guard let page = document.page(at: number) else { throw ScanError.invalidDocument }
            pages.append(try autoreleasepool { try readPage(pdfImage(page)) })
        }
    } else {
        pages.append(try readPage(rasterImage(url)))
    }
    FileHandle.standardOutput.write(try JSONEncoder().encode(pages))
} catch ScanError.lockedPDF {
    exit(2)
} catch ScanError.inputLimit {
    exit(3)
} catch {
    exit(1)
}
