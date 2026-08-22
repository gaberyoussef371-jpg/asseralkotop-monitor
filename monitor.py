import os
import sys
import json
import asyncio
import datetime
import requests
import gspread
import base64
from google.oauth2.service_account import Credentials
from aseeralkotb_product_parser import parse_product_urls

# ==================================================
# CONFIGURATION
# ==================================================
MODE = os.getenv("MONITOR_MODE", "TEST") # "TEST" or "FULL"
TEST_LIMIT = 10
REQUEST_DELAY_SEC = 1.5

# Secrets
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_IDS = os.getenv("TELEGRAM_CHAT_IDS")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
GOOGLE_CREDENTIALS_B64 = os.getenv("GOOGLE_CREDENTIALS_B64")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")

# Sheet Names
SHEET_PRODUCTS = "Products"
SHEET_TEST_LOG = "Monitor Test Log"
SHEET_ERROR_LOG = "Monitor Log"

# ==================================================
# HELPER FUNCTIONS
# ==================================================
def send_telegram_notification(product_name, product_url, changes, publisher):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        return
        
    now = datetime.datetime.now().strftime("%d/%m/%Y %H:%M")
    
    message = f"🔔 *Product Update*\n\n📚 {product_name}\n"
    if publisher:
        message += f"🏢 *Publisher:* {publisher}\n\n"
    else:
        message += "\n"
    
    if "priceBefore" in changes:
        old_val = changes["priceBefore"]["old"] or "N/A"
        new_val = changes["priceBefore"]["new"]
        message += f"💰 *Price Before Sale:*\n{old_val} EGP → {new_val} EGP\n\n"
        
    if "priceAfter" in changes:
        old_val = changes["priceAfter"]["old"] or "N/A"
        new_val = changes["priceAfter"]["new"]
        message += f"🏷 *Sale Price:*\n{old_val} EGP → {new_val} EGP\n\n"
        
    if "stock" in changes:
        old_val = changes["stock"]["old"] or "Unknown"
        new_val = changes["stock"]["new"]
        message += f"📦 *Stock:*\n{old_val} → {new_val}\n\n"
        
    message += f"🔗 *Product:*\n[{product_url}]({product_url})\n\n"
    message += f"⏰ *Checked:*\n{now}"
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    for chat_id in TELEGRAM_CHAT_IDS.split(","):
        chat_id = chat_id.strip()
        if not chat_id:
            continue
        
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }
        
        try:
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            print(f"Failed to send Telegram message to {chat_id}: {e}")

def get_col_index(headers, name, worksheet):
    try:
        # 1-based index for gspread
        return headers.index(name) + 1
    except ValueError:
        # Create column if it doesn't exist
        col_index = len(headers) + 1
        headers.append(name)
        worksheet.update_cell(1, col_index, name)
        return col_index

def setup_test_log(sh):
    try:
        worksheet = sh.worksheet(SHEET_TEST_LOG)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sh.add_worksheet(title=SHEET_TEST_LOG, rows="1000", cols="10")
        headers = ["Timestamp", "Product Name", "Product URL", "Raw Price Data", 
                  "Detected Currency", "Price Before Sale", "Price After Sale", 
                  "Stock Status", "Status", "Error"]
        worksheet.append_row(headers)
    return worksheet

def setup_error_log(sh):
    try:
        worksheet = sh.worksheet(SHEET_ERROR_LOG)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sh.add_worksheet(title=SHEET_ERROR_LOG, rows="1000", cols="6")
        headers = ["Timestamp", "Product Name", "Product URL", "Status", "Error", "HTTP Status"]
        worksheet.append_row(headers)
    return worksheet

# ==================================================
# MAIN LOGIC
# ==================================================
async def main():
    if not (GOOGLE_CREDENTIALS_JSON or GOOGLE_CREDENTIALS_B64) or not SPREADSHEET_ID:
        print("Missing Google credentials or Spreadsheet ID")
        sys.exit(1)
        
    # Setup Google Sheets client
    try:
        if GOOGLE_CREDENTIALS_B64:
            creds_json = base64.b64decode(GOOGLE_CREDENTIALS_B64).decode("utf-8")
        else:
            creds_json = GOOGLE_CREDENTIALS_JSON
        creds_dict = json.loads(creds_json)
    except Exception as e:
        print(f"Failed to parse Google credentials JSON: {e}")
        sys.exit(1)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    
    sh = gc.open_by_key(SPREADSHEET_ID)
    try:
        products_sheet = sh.worksheet(SHEET_PRODUCTS)
    except gspread.exceptions.WorksheetNotFound:
        print(f"Error: Sheet '{SHEET_PRODUCTS}' not found.")
        sys.exit(1)
        
    data = products_sheet.get_all_values()
    if len(data) <= 1:
        print("No data found in Products sheet.")
        return
        
    headers = data[0]
    
    # Get column indices matching the exact screenshot headers
    col_name = get_col_index(headers, "Product Name", products_sheet)
    col_url = get_col_index(headers, "Product URL", products_sheet)
    col_price_before = get_col_index(headers, "Price Before", products_sheet)
    col_price_after = get_col_index(headers, "Price After", products_sheet)
    col_stock = get_col_index(headers, "Stock", products_sheet)
    col_publisher = get_col_index(headers, "publisher", products_sheet)
    
    col_last_checked = get_col_index(headers, "Last Checked", products_sheet)
    col_last_changed = get_col_index(headers, "Last Changed", products_sheet)
    col_monitor_status = get_col_index(headers, "Monitor Status", products_sheet)
    
    limit = min(TEST_LIMIT + 1, len(data)) if MODE == "TEST" else len(data)
    
    test_log = setup_test_log(sh) if MODE == "TEST" else None
    error_log = setup_error_log(sh)
    
    print(f"Running in {MODE} mode. Checking {limit - 1} products...")
    
    # Process products sequentially to respect rate limits
    for i in range(1, limit):
        row = data[i]
        
        # Pad row if it's shorter than headers
        while len(row) < len(headers):
            row.append("")
            
        product_name = row[col_name - 1]
        product_url = row[col_url - 1]
        publisher = row[col_publisher - 1] if col_publisher <= len(row) else ""
        
        if not product_url:
            continue
            
        print(f"Checking: {product_name}...")
        
        # Scrape data using Playwright parser
        try:
            results = await parse_product_urls([product_url], headless=True)
            api_result = results[0]
        except Exception as e:
            api_result = {
                "error": str(e),
                "http_status": 0,
                "parser_status": "fetch_error"
            }
            
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Log to Test Sheet
        if MODE == "TEST":
            raw_price_str = json.dumps(api_result.get("raw_price_value", {}), ensure_ascii=False)
            test_log.append_row([
                now_str,
                product_name,
                product_url,
                raw_price_str,
                api_result.get("detected_currency", "None"),
                api_result.get("price_before_sale", "None"),
                api_result.get("price_after_sale", "None"),
                api_result.get("stock_status", "Unknown"),
                api_result.get("parser_status", "Unknown"),
                api_result.get("error", "")
            ])
            
        # Handle Errors
        if api_result.get("error"):
            error_log.append_row([
                now_str, product_name, product_url, "ERROR", 
                api_result.get("error"), api_result.get("http_status", 0)
            ])
            products_sheet.update_cell(i + 1, col_last_checked, now_str)
            products_sheet.update_cell(i + 1, col_monitor_status, "ERROR")
            continue
            
        # Enforce Currency Safety
        if api_result.get("currency") != "EGP" and api_result.get("parser_status") != "out_of_stock_no_price":
            error_msg = f"Currency error: {api_result.get('parser_status')}"
            error_log.append_row([now_str, product_name, product_url, "ERROR", error_msg, 200])
            products_sheet.update_cell(i + 1, col_last_checked, now_str)
            products_sheet.update_cell(i + 1, col_monitor_status, f"ERROR: {api_result.get('parser_status')}")
            continue
            
        # Change Detection
        old_price_before = row[col_price_before - 1]
        old_price_after = row[col_price_after - 1]
        old_stock = row[col_stock - 1]
        
        new_price_before = api_result.get("price_before_sale")
        new_price_after = api_result.get("price_after_sale")
        new_stock = api_result.get("stock_status")
        
        has_changes = False
        change_details = {}
        
        if new_price_before is not None and str(old_price_before) != str(new_price_before):
            has_changes = True
            change_details["priceBefore"] = {"old": old_price_before, "new": new_price_before}
            
        if new_price_after is not None and str(old_price_after) != str(new_price_after):
            has_changes = True
            change_details["priceAfter"] = {"old": old_price_after, "new": new_price_after}
            
        if new_stock is not None and str(old_stock) != str(new_stock):
            # Normalize old stock status to match new parsed value (e.g. "in stock" vs "In Stock")
            if str(old_stock).lower().strip() != str(new_stock).lower().strip():
                has_changes = True
                change_details["stock"] = {"old": old_stock, "new": new_stock}
            
        # Update Sheet & Notify
        if has_changes:
            print(f"Changes detected for {product_name}!")
            send_telegram_notification(product_name, product_url, change_details, publisher)
            
            updates = []
            if new_price_before is not None:
                updates.append({'range': gspread.utils.rowcol_to_a1(i + 1, col_price_before), 'values': [[new_price_before]]})
            if new_price_after is not None:
                updates.append({'range': gspread.utils.rowcol_to_a1(i + 1, col_price_after), 'values': [[new_price_after]]})
            if new_stock is not None:
                updates.append({'range': gspread.utils.rowcol_to_a1(i + 1, col_stock), 'values': [[new_stock]]})
                
            updates.append({'range': gspread.utils.rowcol_to_a1(i + 1, col_last_changed), 'values': [[now_str]]})
            products_sheet.batch_update(updates)
            
        # Always update status
        products_sheet.update_cell(i + 1, col_last_checked, now_str)
        products_sheet.update_cell(i + 1, col_monitor_status, "OK")
        
        # Rate Limiting
        await asyncio.sleep(REQUEST_DELAY_SEC)
        
    print("Monitoring run complete.")

if __name__ == "__main__":
    asyncio.run(main())
