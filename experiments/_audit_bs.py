import io, os, re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIB = os.path.join(os.path.dirname(ROOT), "WeChat-AI-AutoReply")
P = os.path.join(ROOT, "docs", "dsh-integration.md")
BT = chr(96)

d = io.open(P, encoding="utf-8").read()
lines = d.split("\n")
BS = chr(92)

# 所有反引号里带反斜杠、且像路径的记号
pat = re.escape(BT) + r"([^" + re.escape(BT) + "\n]*" + re.escape(BS) + r"[^" + re.escape(BT) + "\n]*)" + re.escape(BT)
hits = {}
for i, l in enumerate(lines, 1):
    for m in re.findall(pat, l):
        hits.setdefault(m, []).append(i)

print("=== 反斜杠路径记号（逐个解析）===")
bad = []
for tok in sorted(hits):
    t = tok.strip()
    if not re.search(r"[\\/]\w", t):
        continue
    norm = t.replace("/", BS)
    if norm.startswith(".." + BS):
        resolved = os.path.normpath(os.path.join(ROOT, norm))
    elif norm.startswith(BS):
        resolved = norm
    else:
        resolved = os.path.join(ROOT, norm)
        alt = os.path.join(SIB, norm)
        resolved = alt if os.path.exists(alt) else resolved
    ok = os.path.exists(resolved)
    if not ok:
        bad.append((tok, hits[tok], resolved))
    print("  %-3s L%-9s %s" % ("OK" if ok else "!!", ",".join(map(str, hits[tok])), t[:66]))

print("\n=== 解析失败的 ===")
for tok, ls, res in bad:
    print("  L%s  %s" % (",".join(map(str, ls)), tok[:70]))
    print("        -> 解析到: %s" % res)