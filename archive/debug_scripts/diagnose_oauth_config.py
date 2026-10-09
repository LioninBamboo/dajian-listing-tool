"""
eBay OAuth 配置诊断脚本

检查 Streamlit Secrets 配置是否正确
"""

import os

def diagnose_config():
    """诊断 eBay OAuth 配置"""
    print("🔍 eBay OAuth 配置诊断\n")
    print("=" * 70)
    
    # 检查环境变量
    app_id = os.getenv("EBAY_APP_ID", "")
    cert_id = os.getenv("EBAY_CERT_ID", "")
    dev_id = os.getenv("EBAY_DEV_ID", "")
    environment = os.getenv("EBAY_ENVIRONMENT", "")
    redirect_uri = os.getenv("EBAY_REDIRECT_URI", "")
    
    # 显示配置
    print("\n📋 当前配置:\n")
    print(f"EBAY_APP_ID: {app_id[:30]}..." if app_id else "❌ EBAY_APP_ID: 未设置")
    print(f"EBAY_CERT_ID: {cert_id[:30]}..." if cert_id else "❌ EBAY_CERT_ID: 未设置")
    print(f"EBAY_DEV_ID: {dev_id[:30]}..." if dev_id else "❌ EBAY_DEV_ID: 未设置")
    print(f"EBAY_ENVIRONMENT: {environment}")
    print(f"EBAY_REDIRECT_URI: {redirect_uri}\n")
    
    # 检查问题
    issues = []
    
    # 1. 检查必需字段
    if not app_id:
        issues.append("❌ EBAY_APP_ID 未设置")
    if not cert_id:
        issues.append("❌ EBAY_CERT_ID 未设置")
    if not redirect_uri:
        issues.append("❌ EBAY_REDIRECT_URI 未设置")
    
    # 2. 检查 Redirect URI 格式
    if redirect_uri:
        if redirect_uri.endswith('/'):
            issues.append("⚠️ EBAY_REDIRECT_URI 不应该有尾部斜杠")
        if not redirect_uri.startswith('https://'):
            issues.append("❌ EBAY_REDIRECT_URI 必须使用 HTTPS")
        if "streamlit.app" not in redirect_uri:
            issues.append("⚠️ EBAY_REDIRECT_URI 应该是 Streamlit 应用 URL")
    
    # 3. 检查环境匹配
    if environment == "PRODUCTION":
        if "PRD" not in app_id and "PRD" not in cert_id:
            issues.append("⚠️ 环境设置为 PRODUCTION，但凭证可能是 SANDBOX")
    elif environment == "SANDBOX":
        if "PRD" in app_id or "PRD" in cert_id:
            issues.append("⚠️ 环境设置为 SANDBOX，但凭证可能是 PRODUCTION")
    
    # 显示诊断结果
    print("=" * 70)
    print("\n🔍 诊断结果:\n")
    
    if issues:
        print("发现以下问题:\n")
        for i, issue in enumerate(issues, 1):
            print(f"{i}. {issue}")
        print("\n" + "=" * 70)
        print("\n📝 修复建议:\n")
        print("1. 访问 eBay Developer Portal: https://developer.ebay.com/my/keys")
        print("2. 确认使用 Production 环境的凭证")
        print("3. 在 Streamlit Cloud 的 Secrets 中更新配置")
        print("4. 在 eBay Developer Portal 添加 Redirect URI:")
        print(f"   {redirect_uri if redirect_uri else 'https://dajian-listing-tool-votrz8lcr92psxr6bgoxnu.streamlit.app'}")
    else:
        print("✅ 配置看起来正确！\n")
        print("如果仍然授权失败，请检查:")
        print("1. eBay Developer Portal 中是否已添加 Redirect URI")
        print("2. App ID 和 Cert ID 是否完全正确（包括大小写）")
        print("3. 是否使用了正确的 eBay 账号登录")
    
    print("\n" + "=" * 70)

if __name__ == "__main__":
    diagnose_config()
