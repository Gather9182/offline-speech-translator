from pathlib import Path

base = Path("data/train")
en = (base / "rail.en").read_text(encoding="utf-8").splitlines()
de = (base / "rail.de").read_text(encoding="utf-8").splitlines()
assert len(en) == len(de)

K = 50
out_en = []
out_de = []

for i in range(K):
    for j, (e, d) in enumerate(zip(en, de)):
        tag = f" [EX{i:02d}{j:02d}]"
        out_en.append(e + tag)
        out_de.append(d + tag)

(base / "rail_big.en").write_text("\n".join(out_en) + "\n", encoding="utf-8")
(base / "rail_big.de").write_text("\n".join(out_de) + "\n", encoding="utf-8")

print("written", len(out_en), "pairs")
