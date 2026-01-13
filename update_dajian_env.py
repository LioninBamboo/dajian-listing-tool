import os

def update_dajian_creds():
    env_path = ".env"
    
    # New credentials (Production)
    new_key = "a74fa1c8-d7e2-43af-a92a-8145595906fe"
    new_secret = "c7729a8fd5e2418c8e327093ed01e0ab"
    
    with open(env_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
        
    new_lines = []
    key_found = False
    secret_found = False
    url_found = False
    
    for line in lines:
        if line.startswith("DAJIAN_API_KEY="):
            new_lines.append(f"DAJIAN_API_KEY={new_key}\n")
            key_found = True
        elif line.startswith("DAJIAN_API_SECRET="):
            new_lines.append(f"DAJIAN_API_SECRET={new_secret}\n")
            secret_found = True
        elif line.startswith("DAJIAN_BASE_URL="):
            new_lines.append("DAJIAN_BASE_URL=https://open-api.gigacloud.com\n")
            url_found = True
        else:
            new_lines.append(line)
            
    if not key_found:
        new_lines.append(f"DAJIAN_API_KEY={new_key}\n")
    if not secret_found:
        new_lines.append(f"DAJIAN_API_SECRET={new_secret}\n")
    if not url_found:
        new_lines.append("DAJIAN_BASE_URL=https://open-api.gigacloud.com\n")
        
    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
        
    print("Updated .env with Dajian credentials.")

if __name__ == "__main__":
    update_dajian_creds()
