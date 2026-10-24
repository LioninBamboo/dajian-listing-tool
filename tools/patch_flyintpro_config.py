import sys
import os
from pathlib import Path

# 正确的域名是 gigab2b.com (带 'b')
TARGET_RULES = [
    "DOMAIN-SUFFIX,gigacloud.com,Proxy",
    "DOMAIN,api.gigacloud.com,Proxy",
    "DOMAIN,open-api.gigacloud.com,Proxy",
    "DOMAIN-SUFFIX,giga2b.com,Proxy",
    "DOMAIN,openapi.giga2b.com,Proxy",
    # 正确的生产环境域名
    "DOMAIN-SUFFIX,gigab2b.com,Proxy",
    "DOMAIN,openapi.gigab2b.com,Proxy",
    "DOMAIN,openapi-sandbox.gigab2b.com,Proxy",
]

FAKE_IP_FILTER = [
    "gigacloud.com",
    "api.gigacloud.com",
    "open-api.gigacloud.com",
    "giga2b.com",
    "openapi.giga2b.com",
    # 正确的生产环境域名
    "gigab2b.com",
    "openapi.gigab2b.com",
    "openapi-sandbox.gigab2b.com",
]

BANNER = "# === Injected by patch_flyintpro_config.py ===\n"


def insert_rules(text: str) -> str:
    if "rules:" not in text:
        # Append a rules block
        block = "\nrules:\n" + "\n".join([f"  - {r}" for r in TARGET_RULES]) + "\n"
        return text + "\n" + BANNER + block

    lines = text.splitlines()
    out = []
    i = 0
    inserted = False
    while i < len(lines):
        out.append(lines[i])
        if lines[i].strip() == "rules:" and not inserted:
            # find indentation for list items
            indent = "  "
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or not lines[j].lstrip().startswith("- ")):
                j += 1
            if j < len(lines) and lines[j].lstrip().startswith("- "):
                indent = lines[j][:lines[j].index('-')]
            # collect existing rule lines in the rules block until next non-indented section
            # insert our rules after the 'rules:' line if not present
            existing = set()
            k = i + 1
            while k < len(lines):
                line = lines[k]
                if line.strip().startswith("-"):
                    existing.add(line.strip()[2:])
                    k += 1
                    continue
                # stop at first non-list line (likely next section)
                if line and not line.startswith(indent):
                    break
                k += 1
            new_lines = []
            for rule in TARGET_RULES:
                if rule not in existing:
                    new_lines.append(f"{indent}- {rule}")
            if new_lines:
                out.append(BANNER.rstrip())
                out.extend(new_lines)
            inserted = True
        i += 1
    return "\n".join(out) + "\n"


def insert_fake_ip_filter(text: str) -> str:
    if "dns:" not in text:
        # Append a minimal dns block
        block = (
            "\ndns:\n"
            "  enable: true\n"
            "  fake-ip-filter:\n" + "\n".join([f"    - {d}" for d in FAKE_IP_FILTER]) + "\n"
        )
        return text + "\n" + BANNER + block

    lines = text.splitlines()
    out = []
    i = 0
    inserted = False
    while i < len(lines):
        out.append(lines[i])
        if lines[i].strip() == "dns:" and not inserted:
            # find indentation
            indent = "  "
            # check if fake-ip-filter exists
            has_filter = False
            j = i + 1
            while j < len(lines) and lines[j].startswith(indent):
                if lines[j].strip().startswith("fake-ip-filter:"):
                    has_filter = True
                    break
                j += 1
            if not has_filter:
                out.append(f"{indent}fake-ip-filter:")
                out.extend([f"{indent}  - {d}" for d in FAKE_IP_FILTER])
            inserted = True
        i += 1
    return "\n".join(out) + "\n"


def main():
    if len(sys.argv) < 2:
        print("Usage: python tools/patch_flyintpro_config.py <config.yaml>")
        print("Tip: In FlyintPro, export your profile/config YAML, then run this to inject rules.")
        sys.exit(1)
    cfg_path = Path(sys.argv[1])
    if not cfg_path.exists():
        print(f"Config not found: {cfg_path}")
        sys.exit(1)
    text = cfg_path.read_text(encoding="utf-8", errors="ignore")
    backup = cfg_path.with_suffix(cfg_path.suffix + ".bak")
    backup.write_text(text, encoding="utf-8")
    text = insert_rules(text)
    text = insert_fake_ip_filter(text)
    cfg_path.write_text(text, encoding="utf-8")
    print(f"Patched config written: {cfg_path}")
    print(f"Backup saved: {backup}")
    print("Injected rules:")
    for r in TARGET_RULES:
        print(f"  - {r}")
    print("Injected dns.fake-ip-filter entries:")
    for d in FAKE_IP_FILTER:
        print(f"  - {d}")

if __name__ == "__main__":
    main()
