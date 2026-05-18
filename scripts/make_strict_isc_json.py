from pathlib import Path
import json

SRC = Path("data/samples/isc_20251_sample.json")
DST = Path("data/samples/isc_20251_strict_rooms.json")

THEORY = [
    "FF1","FF2","FF3","FF4","FF5","FF6","FF7","FF8","FF9",
    "FFA","FFB","FFC","FFD"
]

LABS = [
    "LCC","LAM","K11","K12","K13","K14","L12","L13","L14"
]

ALLOWED = set(THEORY + LABS)

with SRC.open("r", encoding="utf-8") as f:
    data = json.load(f)

old_A = data.get("A", [])
old_AT = data.get("AT", [])
old_AL = data.get("AL", [])

new_AT = [r for r in THEORY if r in old_A or r in old_AT]
new_AL = [r for r in LABS if r in old_A or r in old_AL]
new_A = new_AT + new_AL

data["A"] = new_A
data["AT"] = new_AT
data["AL"] = new_AL

# Prune common room-related dictionaries if they exist
possible_room_dicts = [
    "C",
    "Cap",
    "cap",
    "capacidad",
    "capacidades",
    "capacidad_aula",
    "room_capacity",
    "room_capacities",
    "tipo_aula",
    "room_type",
    "room_types",
    "tipo",
    "A_type",
    "A_cap",
]

for key in possible_room_dicts:
    value = data.get(key)
    if isinstance(value, dict):
        data[key] = {
            k: v for k, v in value.items()
            if str(k).upper() in ALLOWED
        }

# Also prune nested metadata dictionaries if they are room-indexed
for key, value in list(data.items()):
    if isinstance(value, dict):
        keys = {str(k).upper() for k in value.keys()}
        if keys and keys.issubset({str(r).upper() for r in old_A}):
            data[key] = {
                k: v for k, v in value.items()
                if str(k).upper() in ALLOWED
            }

meta = data.setdefault("metadata", {})
meta["strict_room_filter"] = True
meta["strict_theory_rooms"] = new_AT
meta["strict_lab_rooms"] = new_AL
meta["source_json"] = str(SRC)

DST.parent.mkdir(parents=True, exist_ok=True)

with DST.open("w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print("Strict JSON written:", DST)
print("Old |A|:", len(old_A), "Old |AT|:", len(old_AT), "Old |AL|:", len(old_AL))
print("New |A|:", len(new_A), "New |AT|:", len(new_AT), "New |AL|:", len(new_AL))
print("New AT:", new_AT)
print("New AL:", new_AL)

missing_theory = [r for r in THEORY if r not in new_AT]
missing_labs = [r for r in LABS if r not in new_AL]

if missing_theory:
    print("WARNING missing theory rooms:", missing_theory)

if missing_labs:
    print("WARNING missing lab rooms:", missing_labs)
