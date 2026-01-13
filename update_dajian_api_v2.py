import os
from dotenv import load_dotenv, set_key

def update_dajian_credentials():
    """更新大建云仓 API 2.0 凭证"""
    env_path = ".env"
    
    # 新的凭证（生产环境）
    new_credentials = {
        "DAJIAN_API_KEY": "a74fa1c8-d7e2-43af-a92a-8145595906fe",
        "DAJIAN_API_SECRET": "c7729a8fd5e2418c8e327093ed01e0ab",
        "DAJIAN_BASE_URL": "https://open-api.gigacloud.com"  # 可能需要根据文档调整
    }
    
    print("更新大建云仓 API 凭证...")
    for key, value in new_credentials.items():
        set_key(env_path, key, value)
        print(f"✓ {key} 已更新")
    
    print("\n✓ 凭证更新完成！")
    print("注意：请查看 API 2.0 文档确认 Base URL 是否正确")

if __name__ == "__main__":
    update_dajian_credentials()
