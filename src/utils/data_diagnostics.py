import json, os, re, sys

JSON = sys.argv[1] if len(sys.argv)>1 else "salidas/datos_modelo_20241.json"

LAB_RE  = re.compile(os.getenv("LAB_COURSE_REGEX", r"(?i)\b(LAB|LABORATORI|TALLER|PR(A|Á)CTIC)\b"))
DAYS    = 5   # si en tu JSON vienen D y H diferentes, cámbialo o léelo del JSON
HOURS   = 6

with open(JSON, "r", encoding="utf-8") as f:
    d = json.load(f)

P,A,AT,AL = d["P"], d.get("A",[]), d.get("AT",[]), d.get("AL",[])
D,H       = d.get("D", ["L","M","X","J","V"]), d.get("H", [1,2,3,4,5,6])
MG,Hreq   = d["MG"], d["Hreq"]

days  = len(D) or DAYS
hours = len(H) or HOURS
cap_AT = len(AT) * days * hours
cap_AL = len(AL) * days * hours

dem_total = 0
dem_T = 0
dem_L = 0
for m,g in (tuple(x) if isinstance(x,(list,tuple)) else tuple(str(x).split("|",1)) for x in MG):
    h = int(Hreq.get(f"{m}|{g}",0))
    dem_total += h
    if LAB_RE.search(m):
        dem_L += h
    else:
        dem_T += h

print(f"|A|={len(A)} |AT|={len(AT)} |AL|={len(AL)}  D={days} H={hours}")
print(f"Capacidad AT: {cap_AT}  Capacidad AL: {cap_AL}  Capacidad total: {cap_AT+cap_AL}")
print(f"Demanda total (ΣHreq): {dem_total}")
print(f"Demanda T: {dem_T} vs Cap_AT")
print(f"Demanda L: {dem_L} vs Cap_AL")
