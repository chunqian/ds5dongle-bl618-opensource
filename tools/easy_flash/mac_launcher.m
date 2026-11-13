// SPDX-License-Identifier: GPL-3.0-or-later
#import <Cocoa/Cocoa.h>

int main(void) {
    @autoreleasepool {
        NSApplication *app = [NSApplication sharedApplication];
        [app setActivationPolicy:NSApplicationActivationPolicyAccessory];
        NSURL *script = [[NSBundle mainBundle] URLForResource:@"Run" withExtension:@"command"];
        NSURL *terminal = [NSURL fileURLWithPath:@"/System/Applications/Utilities/Terminal.app"];
        NSWorkspaceOpenConfiguration *config = [NSWorkspaceOpenConfiguration configuration];
        [[NSWorkspace sharedWorkspace] openURLs:@[script] withApplicationAtURL:terminal
            configuration:config completionHandler:^(NSRunningApplication *running, NSError *error) {
                (void)running;
                dispatch_async(dispatch_get_main_queue(), ^{
                    if (error) {
                        NSAlert *alert = [[NSAlert alloc] init];
                        alert.messageText = @"无法打开烧录窗口";
                        alert.informativeText = error.localizedDescription;
                        [alert runModal];
                    }
                    [app terminate:nil];
                });
            }];
        [app run];
    }
    return 0;
}
