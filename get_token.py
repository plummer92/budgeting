import os
import time
from plaid import Configuration, ApiClient
from plaid.api import plaid_api
from plaid.model.products import Products
from plaid.model.country_code import CountryCode
from plaid.model.sandbox_public_token_create_request import SandboxPublicTokenCreateRequest
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest

# Read secrets from environment
client_id = os.getenv("PLAID_CLIENT_ID")
secret = os.getenv("PLAID_SECRET")

if not client_id or not secret:
    print("❌ Error: Secrets not found. Did you add them to Codespace Secrets?")
    exit()

# 1. Setup Plaid
config = Configuration(
    host="https://sandbox.plaid.com",
    api_key={"clientId": client_id, "secret": secret}
)
api_client = ApiClient(config)
client = plaid_api.PlaidApi(api_client)

def get_access_token():
    print("--- 1. Creating a Fake 'Public Token' (Simulating Login)... ---")
    
    # Create a sandbox public token (simulates a user logging into Chase/Wells Fargo)
    pt_request = SandboxPublicTokenCreateRequest(
        institution_id="ins_109508", # First Sandbox Bank
        initial_products=[Products("transactions")],
        options={"webhook": "https://www.genericwebhookurl.com/webhook"}
    )
    
    try:
        pt_response = client.sandbox_public_token_create(pt_request)
        public_token = pt_response.public_token
        print(f"✅ Public Token Created!")
    except Exception as e:
        print(f"❌ Error creating public token: {e}")
        return

    print("--- 2. Exchanging for Permanent Access Token... ---")
    
    # Exchange public token for access token
    exchange_request = ItemPublicTokenExchangeRequest(public_token=public_token)
    
    try:
        exchange_response = client.item_public_token_exchange(exchange_request)
        access_token = exchange_response.access_token
        print("\n🎉 SUCCESS! Here is your Access Token:")
        print("---------------------------------------------------")
        print(access_token)
        print("---------------------------------------------------")
        print(">> Save this token! You will use it in your main script.")
    except Exception as e:
        print(f"❌ Error exchanging token: {e}")

if __name__ == "__main__":
    get_access_token()
