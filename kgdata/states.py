"""US states, DC and territories: postal code <-> FIPS code."""

from __future__ import annotations

STATES = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08", "CT": "09",
    "DE": "10", "DC": "11", "FL": "12", "GA": "13", "HI": "15", "ID": "16", "IL": "17",
    "IN": "18", "IA": "19", "KS": "20", "KY": "21", "LA": "22", "ME": "23", "MD": "24",
    "MA": "25", "MI": "26", "MN": "27", "MS": "28", "MO": "29", "MT": "30", "NE": "31",
    "NV": "32", "NH": "33", "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38",
    "OH": "39", "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46",
    "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53", "WV": "54",
    "WI": "55", "WY": "56",
    # territories
    "AS": "60", "GU": "66", "MP": "69", "PR": "72", "VI": "78",
}
FIPS_TO_STATE = {v: k for k, v in STATES.items()}


def resolve(states) -> list[str]:
    """'all' / None -> every code; otherwise a list of postal codes (FIPS codes accepted)."""
    if states is None or (isinstance(states, str) and states.lower() == "all"):
        return list(STATES)
    if isinstance(states, str):
        states = [states]
    out = []
    for s in states:
        s = str(s).upper().replace("US:", "")
        out.append(FIPS_TO_STATE.get(s.zfill(2), s) if s.isdigit() else s)
    unknown = [s for s in out if s not in STATES]
    if unknown:
        raise ValueError(f"unknown state codes: {unknown}")
    return out
