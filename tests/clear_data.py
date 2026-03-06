import os
from dotenv import load_dotenv
load_dotenv()
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = r"C:\Users\Jakaria.Ahmed\.gemini\antigravity\agent-gcp-sa-full-access.json"

from googleapiclient.discovery import build
import google.auth
from google.cloud import firestore

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "gen-lang-client-0746536657")
db = firestore.Client(project=PROJECT_ID)

def clear_firestore():
    print("Clearing Firestore...")
    collections = ['system', 'stats', 'trackers']
    for collection_name in collections:
        docs = db.collection(collection_name).stream()
        for doc in docs:
            # Check for subcollections
            if collection_name == 'trackers':
                sub_docs = doc.reference.collection("logs").stream()
                for sub_doc in sub_docs:
                    sub_doc.reference.delete()
            doc.reference.delete()
    print("Firestore cleared successfully.")

def clear_sheets():
    print("Clearing Google Sheets...")
    credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/spreadsheets'])
    sheets_service = build('sheets', 'v4', credentials=credentials)
    
    sheet_metadata = sheets_service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    sheets = sheet_metadata.get('sheets', '')
    
    requests = []
    
    for i, sheet in enumerate(sheets):
        sheet_id = sheet.get("properties").get("sheetId")
        title = sheet.get("properties").get("title")
        
        if i == 0:
            # Rename first sheet to something clean and clear all cells
            requests.append({
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": sheet_id,
                        "title": "Sheet1"
                    },
                    "fields": "title"
                }
            })
            requests.append({
                "updateCells": {
                    "range": {
                        "sheetId": sheet_id
                    },
                    "fields": "userEnteredValue"
                }
            })
        else:
            # Delete all other sheets
            requests.append({
                "deleteSheet": {
                    "sheetId": sheet_id
                }
            })
            
    if requests:
        body = {'requests': requests}
        sheets_service.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID, body=body).execute()
    print("Google Sheets cleared successfully.")

if __name__ == "__main__":
    clear_firestore()
    clear_sheets()
