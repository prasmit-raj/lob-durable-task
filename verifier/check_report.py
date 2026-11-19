"""Pass only if the JUnit report has exactly the expected tests, all passed."""
import sys
import xml.etree.ElementTree as ET

report, expected = sys.argv[1], int(sys.argv[2])
cases = list(ET.parse(report).getroot().iter("testcase"))
bad = [c.get("name") for c in cases if any(ch.tag in ("failure", "error", "skipped") for ch in c)]
print(f"{len(cases)} tests ran, {len(bad)} not passed, {expected} expected")
for name in bad:
    print(f"  not passed: {name}")
sys.exit(0 if len(cases) == expected and not bad else 1)
