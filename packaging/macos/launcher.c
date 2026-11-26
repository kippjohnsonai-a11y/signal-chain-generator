// The app's main executable: run launcher.sh from the bundle's Resources.
// A compiled stub (rather than a script) so the bundle signs and notarizes
// like any other app.
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

int main(void) {
    char exe[PATH_MAX], real[PATH_MAX], script[PATH_MAX];
    uint32_t size = sizeof exe;
    if (_NSGetExecutablePath(exe, &size) != 0 || realpath(exe, real) == NULL) {
        return 1;
    }
    // .../Contents/MacOS/<this> -> .../Contents/Resources/launcher.sh
    char *macos = dirname(real);
    snprintf(script, sizeof script, "%s/../Resources/launcher.sh", macos);
    execl("/bin/bash", "bash", script, (char *)NULL);
    perror("bash");
    return 1;
}
