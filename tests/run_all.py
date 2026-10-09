"""
Runs every test script in this folder, one after another, and prints a summary.
    python3 tests/run_all.py            (from the project root, or from anywhere)
Each script is a plain program that prints what it checked; it passes when it exits
normally and doesn't report problems.
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# Quick ones first; the full-run scripts (scenario, fuzz, persistence, api) take longest
ORDER = [
    "test_bindings.py", "test_market_loader.py", "test_forwards.py", "test_pricer_with_curves.py",
    "test_cpp_strikes.py", "test_sabr.py", "test_smile_vol.py", "test_market_snapshot.py",
    "test_book.py", "test_hedging.py", "test_pnl.py", "test_session.py",
    "fuzz_hedging.py", "scenario_test.py", "test_persistence.py", "test_api.py",
]


def failed(output, code):
    if code != 0:
        return True
    words = ["FAIL", "PROBLEMS", "Traceback"]
    for word in words:
        if word in output:
            return True
    return False


def main():
    scripts = []
    for name in ORDER:
        if os.path.exists(os.path.join(HERE, name)):
            scripts.append(name)
    # anything new that isn't in ORDER yet
    for name in sorted(os.listdir(HERE)):
        if name.endswith(".py") and name != "run_all.py" and name not in scripts:
            scripts.append(name)

    results = []
    for name in scripts:
        started = time.time()
        run = subprocess.run([sys.executable, os.path.join(HERE, name)], capture_output=True, text=True)
        seconds = time.time() - started
        output = run.stdout + run.stderr
        bad = failed(output, run.returncode)
        last_line = ""
        lines = output.strip().splitlines()
        if len(lines) > 0:
            last_line = lines[-1]
        print(f"{'FAIL' if bad else 'ok  '}  {name:28} {seconds:6.1f}s  {last_line[:70]}")
        if bad:
            print(output[-3000:])
        results.append(bad)

    failures = 0
    for bad in results:
        if bad:
            failures += 1
    print()
    print(f"{len(results) - failures} of {len(results)} scripts passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()