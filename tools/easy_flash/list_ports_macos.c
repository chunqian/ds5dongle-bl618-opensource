/* SPDX-License-Identifier: GPL-3.0-or-later */
/* List only native BL616 ROM download ports, never arbitrary serial devices. */
#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOKitLib.h>
#include <stdio.h>
#include <string.h>

static int number(io_registry_entry_t entry, CFStringRef key)
{
    CFTypeRef value = IORegistryEntryCreateCFProperty(entry, key, kCFAllocatorDefault, 0);
    int result = -1;
    if (value && CFGetTypeID(value) == CFNumberGetTypeID())
        CFNumberGetValue(value, kCFNumberIntType, &result);
    if (value) CFRelease(value);
    return result;
}

static int is_download_port(io_registry_entry_t serial)
{
    io_registry_entry_t entry = serial, parent;
    IOObjectRetain(entry);
    while (entry) {
        int match = number(entry, CFSTR("idVendor")) == 0x349b &&
                    number(entry, CFSTR("idProduct")) == 0x6160;
        if (match) { IOObjectRelease(entry); return 1; }
        kern_return_t status = IORegistryEntryGetParentEntry(entry, kIOServicePlane, &parent);
        IOObjectRelease(entry);
        entry = status == KERN_SUCCESS ? parent : 0;
    }
    return 0;
}

int main(void)
{
    io_iterator_t iterator;
    if (IOServiceGetMatchingServices(MACH_PORT_NULL,
            IOServiceMatching("IOSerialBSDClient"), &iterator) != KERN_SUCCESS) {
        fputs("Cannot enumerate USB serial ports.\n", stderr);
        return 1;
    }
    io_service_t entry;
    while ((entry = IOIteratorNext(iterator))) {
        if (is_download_port(entry)) {
            CFTypeRef value = IORegistryEntryCreateCFProperty(entry,
                CFSTR("IOCalloutDevice"), kCFAllocatorDefault, 0);
            char port[1024];
            if (value && CFGetTypeID(value) == CFStringGetTypeID() &&
                CFStringGetCString(value, port, sizeof(port), kCFStringEncodingUTF8) &&
                strncmp(port, "/dev/cu.", 8) == 0)
                puts(port);
            if (value) CFRelease(value);
        }
        IOObjectRelease(entry);
    }
    IOObjectRelease(iterator);
    return 0;
}
