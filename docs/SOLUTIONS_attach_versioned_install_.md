import os

def main():
    """
    Determines the target version tag, defaulting to 'v0.0.1' if not found 
    in environment variables (e.g., in a GitHub Actions run).
    """
    # The active tag (e.g., v2.3.4) is usually passed via GITHUB_REF_NAME
    TARGET_TAG = os.environ.get("GITHUB_REF_NAME", "v0.0.1") 

    print(f"Target Version Tag set to: {TARGET_TAG}")

if __name__ == "__main__":
    main()