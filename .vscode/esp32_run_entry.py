"""Entry point used by VS Code's Python Run button.

The preLaunchTask uploads the six PCB modules. This launcher resets the board
and executes the local main.py through mpremote, keeping its stdout attached
from the first program log line.
"""

import subprocess
import sys
import time

if __name__ == "__main__":
    port = sys.argv[1] if len(sys.argv) > 1 else "COM5"
    main_file = sys.argv[2] if len(sys.argv) > 2 else "main.py"
    def invoke(command):
        return subprocess.call([sys.executable, "-m", "mpremote", "connect", port] + command)

    # The task's preceding reset normally already ran. This second reset makes
    # the standalone run task consistent with the VS Code Run button.
    if invoke(["reset"]) != 0:
        raise SystemExit("ESP32 reset failed. Close Thonny/serial terminals using %s, then retry." % port)
    time.sleep(1.5)  # ESP32 USB CDC needs time to re-enumerate after reset.
    print("Starting main.py on %s; Ctrl+C interrupts firmware." % port, flush=True)
    for attempt in range(1, 4):
        result = invoke(["run", main_file])
        if result == 0:
            raise SystemExit(0)
        if attempt < 3:
            print("COM port not ready; retrying (%d/3)..." % (attempt + 1), flush=True)
            time.sleep(1.5)
    raise SystemExit("Unable to open %s. Close Thonny, any mpremote REPL, and VS Code serial-monitor terminals, then retry." % port)
