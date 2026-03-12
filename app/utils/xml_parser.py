import xml.etree.ElementTree as ET
from pathlib import Path


SCA_NAMESPACE = "http://docs.oasis-open.org/ns/opencsa/sca/200912"
NS = {"sca": SCA_NAMESPACE}


def parse_component_names(composite_path: str) -> list[str]:
    """Parse component names from an ECOA composite file."""
    composite_file = Path(composite_path)
    if not composite_file.is_file():
        raise FileNotFoundError(f"Composite file not found: {composite_path}")

    try:
        tree = ET.parse(composite_file)
    except ET.ParseError as exc:
        raise ValueError(
            f"Failed to parse XML composite file '{composite_path}': {exc}"
        ) from exc

    root = tree.getroot()
    component_names = [
        component.get("name")
        for component in root.findall(".//sca:component", NS)
        if component.get("name")
    ]

    # Deduplicate while preserving declaration order in the composite.
    return list(dict.fromkeys(component_names))
