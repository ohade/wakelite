import AppKit
import UserNotifications

// WakeLiteNotify — posts a WakeLite desktop notification that opens the
// dashboard when clicked.
//
// Why this bundle exists: AppleScript's `display notification` carries no
// click action, and macOS attributes an osascript notification to Script
// Editor, so clicking a WakeLite alert opened Script Editor's document picker.
// Homebrew's terminal-notifier can carry a click target, but on macOS 26 its
// adhoc-signed bundle is refused notification permission outright. Posting
// from an app WakeLite owns gives the notification its own bundle identity,
// its own permission grant, and a delegate that can act on the click.
//
// Two modes:
//   post  — WakeLiteNotify --title T --message M [--url U] [--group G]
//   click — no arguments. macOS relaunches this app when one of its
//           notifications is clicked; the delegate opens the URL that was
//           carried in the notification's userInfo.

let center = UNUserNotificationCenter.current()

func argument(_ name: String) -> String? {
    let args = CommandLine.arguments
    guard let index = args.firstIndex(of: "--\(name)"), index + 1 < args.count else { return nil }
    return args[index + 1]
}

func fail(_ message: String, code: Int32) -> Never {
    FileHandle.standardError.write(Data("WakeLiteNotify: \(message)\n".utf8))
    exit(code)
}

// MARK: - click mode

final class ClickHandler: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    // The delegate must be in place before AppKit finishes launching, or the
    // response that caused this launch is delivered to nobody.
    func applicationWillFinishLaunching(_ notification: Notification) {
        center.delegate = self
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Launched by hand, or the response was already consumed. Do not
        // linger as an invisible process.
        DispatchQueue.main.asyncAfter(deadline: .now() + 10) { exit(0) }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        if let raw = response.notification.request.content.userInfo["url"] as? String,
           let url = URL(string: raw) {
            NSWorkspace.shared.open(url)
        }
        completionHandler()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { exit(0) }
    }
}

// MARK: - post mode

func post(title: String, message: String, url: String?, group: String?) -> Never {
    var granted = false
    let authorization = DispatchSemaphore(value: 0)
    center.requestAuthorization(options: [.alert, .sound]) { ok, error in
        granted = ok
        if let error = error {
            FileHandle.standardError.write(Data("WakeLiteNotify: \(error.localizedDescription)\n".utf8))
        }
        authorization.signal()
    }
    // A denial or a hang must exit non-zero, so WakeLite falls back to
    // osascript rather than silently dropping the alert.
    guard authorization.wait(timeout: .now() + 10) == .success else {
        fail("timed out requesting notification permission", code: 2)
    }
    guard granted else {
        fail("notification permission not granted", code: 2)
    }

    let content = UNMutableNotificationContent()
    content.title = title
    content.body = message
    if let url = url { content.userInfo = ["url": url] }
    if let group = group { content.threadIdentifier = group }

    // Reusing the group as the request identifier means a repeatedly failing
    // timer replaces its own previous alert instead of stacking up.
    let identifier = group ?? UUID().uuidString
    var deliveryError: Error?
    let delivery = DispatchSemaphore(value: 0)
    center.add(UNNotificationRequest(identifier: identifier, content: content, trigger: nil)) { error in
        deliveryError = error
        delivery.signal()
    }
    guard delivery.wait(timeout: .now() + 10) == .success else {
        fail("timed out delivering the notification", code: 3)
    }
    if let deliveryError = deliveryError {
        fail(deliveryError.localizedDescription, code: 3)
    }
    exit(0)
}

// MARK: - entry point

if let title = argument("title"), let message = argument("message") {
    post(title: title, message: message, url: argument("url"), group: argument("group"))
} else {
    let app = NSApplication.shared
    let handler = ClickHandler()
    app.delegate = handler
    app.setActivationPolicy(.accessory)
    app.run()
}
