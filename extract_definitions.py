"""One-off helper: turn the CMS data dictionary PDFs into data/cms_definitions.json.

Usage: uv run extract_definitions.py   (needs `pdftotext`, from poppler-utils)

build_mappings.py feeds these real definitions to the model when it writes column
descriptions, so descriptions come from CMS, not from the model's guesses.
The output is committed, so you only rerun this if you swap the PDFs.
"""

import json
import re
import subprocess
from pathlib import Path

DOCS = Path(__file__).parent / "docs"  # put the four *-datadictionary-py27.pdf files here
OUT = Path(__file__).parent / "data" / "cms_definitions.json"
FILES = {
    "rate": "rate-datadictionary-py27.pdf",
    "plan_attributes": "planattributes-datadictionary-py27.pdf",
    "service_area": "servicearea-datadictionary-py27.pdf",
    "crosswalk": "plan-id-crosswalk-datadictionary-py27.pdf",
}
NOISE = re.compile(r"^\s*(\d+|The Center for Consumer Information.*|.*Public Use File Data Dictionary)\s*$")


def clean(lines):
    return " ".join(" ".join(l.split()) for l in lines if l.strip() and not NOISE.match(l)).strip()


def parse(text):
    out = {}
    for block in re.split(r"\n\s*Variable Name:\s+", "\n" + text)[1:]:
        name, _, rest = block.partition("\n")
        name = name.strip()
        fields, key = {}, None
        for line in rest.splitlines():
            m = re.match(r"\s*(Variable Definition|Data Type|Variable Label|Allowable Values|Data Source|Field Name from( Data Source)?|Comments):\s*(.*)", line)
            if m:
                key = m.group(1).split(" from")[0]
                fields[key] = [m.group(3)]
            elif key:
                fields[key].append(line)
        out[name] = {
            "definition": clean(fields.get("Variable Definition", [])),
            "comments": clean(fields.get("Comments", [])),
        }
    return out


if __name__ == "__main__":
    result = {}
    for kind, pdf in FILES.items():
        text = subprocess.run(["pdftotext", "-layout", str(DOCS / pdf), "-"], capture_output=True, text=True, check=True).stdout
        result[kind] = parse(text)
        print(kind, len(result[kind]), "variables")
    OUT.write_text(json.dumps(result, indent=1))
