import json
import boto3
import base64
import uuid
import re
from decimal import Decimal
from datetime import datetime

# אתחול שירותי AWS
s3 = boto3.client('s3')
rekognition = boto3.client('rekognition')
dynamodb = boto3.resource('dynamodb')

BUCKET_NAME = 'smartreceipt-uploads-final-2026'
TABLE_NAME = 'Receipts'

# כותרות CORS אחידות לכל תשובה
CORS_HEADERS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
    'Content-Type': 'application/json'
}

# ---------------------------------------------------------------------------
# סיווג קבלות מעודכן לפי הדרישות החדשות
# ---------------------------------------------------------------------------
CATEGORY_KEYWORDS = {
    'Marketing and Advertising': [
        'facebook', 'google', 'ads', 'instagram', 'taboola', 'outbrain', 'linkedin',
        'marketing', 'advertising', 'promo', 'campaign', 'meta', 'tiktok', 'adroll',
        'mailchimp', 'hubspot', 'copywriter', 'seo', 'ppc', 'agency', 'creative'
    ],
    'Food and Hospitality': [
        'market', 'grocery', 'supermarket', 'cafe', 'coffee', 'restaurant', 'bar',
        'catering', 'bakery', 'food', 'beverage', 'walmart', 'wolt', 'ubereats',
        'starbucks', 'espresso', 'kitchen', 'hospitality', 'dinner', 'lunch', 'breakfast'
    ],
    'Travel and Transportation': [
        'fuel', 'gas', 'station', 'parking', 'train', 'taxi', 'cab', 'uber', 'gett',
        'lyft', 'flight', 'airline', 'hotel', 'rent', 'car', 'travel', 'toll', 'garage',
        'paz', 'sonol', 'delek', 'yellow'
    ],
    'IT and Equipment': [
        'computer', 'laptop', 'hardware', 'software', 'cloud', 'aws', 'microsoft',
        'apple', 'macbook', 'iphone', 'samsung', 'monitor', 'keyboard', 'mouse',
        'office', 'depot', 'supplies', 'paper', 'ink', 'printer', 'ksp', 'bug', 'hosting',
        'domain', 'godaddy', 'github', 'zoom', 'slack', 'atlassian', 'jira'
    ],
    'Maintenance and General Expenses': [
        'electricity', 'water', 'internet', 'telecom', 'phone', 'cell', 'maintenance',
        'cleaning', 'repair', 'tools', 'rent', 'insurance', 'tax', 'fee', 'service',
        'subscription', 'support', 'delivery', 'shipping', 'fedex', 'ups', 'dhl'
    ]
}

DEFAULT_CATEGORY = 'Maintenance and General Expenses'

class DecimalEncoder(json.JSONEncoder):
    """מאפשר להמיר ערכי Decimal של DynamoDB ל-JSON בלי שגיאות."""
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def respond(status_code, body_dict):
    """עוטף כל תשובה בכותרות CORS וב-JSON."""
    return {
        'statusCode': status_code,
        'headers': CORS_HEADERS,
        'body': json.dumps(body_dict, cls=DecimalEncoder, ensure_ascii=False)
    }


def get_http_method(event):
    """מזהה את שיטת ה-HTTP גם ב-REST API (v1) וגם ב-HTTP API (v2)."""
    if 'httpMethod' in event:
        return event['httpMethod']
    return (event.get('requestContext', {})
                 .get('http', {})
                 .get('method', 'POST'))


def classify_receipt(text_pool):
    """מחזיר את הקטגוריה עם הכי הרבה התאמות מילות מפתח."""
    scores = {}
    for category, keywords in CATEGORY_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw in text_pool)
        if hits > 0:
            scores[category] = hits
    if not scores:
        return DEFAULT_CATEGORY
    return max(scores, key=scores.get)


def validate_and_extract(lines):
    """
    בודק האם זו קבלה ומחלץ: שם עסק, סכום, מע"מ, אמצעי תשלום וקטגוריה.
    מתמודד עם רווחים בין אותיות (כמו 'ל ת ש ל ו ם').
    """
    text_pool = " ".join(lines).lower()
    text_pool_clean = text_pool.replace(" ", "")

    # 1. מנגנון הגנה: בדיקה האם התמונה היא בכלל קבלה
    receipt_keywords = ['סך', 'סה"כ', 'סהכ', 'לתשלום', 'חשבונית', 'קבלה', 'מע"מ', 'מעמ',
                        'פריט', 'מחיר', 'קופה', 'ש"ח', 'total', 'receipt', 'invoice',
                        'amount', 'tax', 'cash', 'visa']
    
    matches = [kw for kw in receipt_keywords if kw in text_pool or kw.replace(" ", "") in text_pool_clean]

    if len(lines) < 3 or len(matches) < 1:
        return False, {}, "הקובץ שהועלה אינו נראה כמו קבלה תקינה."

    # 2. חילוץ הנתונים
    merchant_name = lines[0].strip() if len(lines[0].strip()) > 1 else lines[1].strip()
    total_amount = "0.00"
    vat_amount = "0.00"
    payment_method = "לא זוהה"

    if any(kw in text_pool for kw in ['אשראי', 'כרטיס', 'ויזה', 'visa', 'mastercard', 'credit', 'ישראכרט']):
        payment_method = "כרטיס אשראי"
    elif any(kw in text_pool for kw in ['מזומן', 'cash', 'עודף']):
        payment_method = "מזומן"

    # לולאה לחילוץ סכומים
    for i, line in enumerate(lines):
        line_lower = line.lower()
        line_no_spaces = line_lower.replace(" ", "")

        # חילוץ סכום כולל
        if total_amount == "0.00" and any(kw in line_no_spaces for kw in ['סךהכל', 'סך-הכל', 'סה"כ', 'סהכ', 'לתשלום', 'total', 'amount']):
            text_to_search = line + (" " + lines[i + 1] if i + 1 < len(lines) else "")
            numbers = re.findall(r'\d+\.\d{2}|\d+\.\d+|\d+', text_to_search)
            if numbers:
                total_amount = numbers[-1]

        # חילוץ מע"מ
        if vat_amount == "0.00" and any(kw in line_no_spaces for kw in ['מע"מ', 'מעמ', 'vat', 'כוללמעמ']):
            text_to_search = line + (" " + lines[i + 1] if i + 1 < len(lines) else "")
            numbers = re.findall(r'\d+\.\d{2}|\d+\.\d+|\d+', text_to_search)
            if numbers:
                vat_amount = numbers[-1]

    # 3. סיווג הקבלה לקטגוריה החדשה
    category = classify_receipt(text_pool)

    extracted_data = {
        'merchant': merchant_name,
        'total': total_amount,
        'vat': vat_amount,
        'payment_method': payment_method,
        'category': category
    }

    return True, extracted_data, None


def get_all_receipts():
    """סורק את כל הקבלות מ-DynamoDB ומחזיר אותן ממוינות מהחדש לישן."""
    table = dynamodb.Table(TABLE_NAME)
    response = table.scan()
    items = response.get('Items', [])

    while 'LastEvaluatedKey' in response:
        response = table.scan(ExclusiveStartKey=response['LastEvaluatedKey'])
        items.extend(response.get('Items', []))

    items.sort(key=lambda x: x.get('UploadDate', ''), reverse=True)
    return items


def handle_upload(event):
    """מטפל בהעלאת קבלה חדשה (POST)."""
    body = json.loads(event['body'])
    
    # ניקוי ה-Base64 מקידומות ה-Frontend
    image_base64 = body['image']
    if ',' in image_base64:
        image_base64 = image_base64.split(',')[1]

    image_data = base64.b64decode(image_base64)

    # ניתוח התמונה
    rekognition_response = rekognition.detect_text(Image={'Bytes': image_data})
    lines = [t['DetectedText'] for t in rekognition_response['TextDetections'] if t['Type'] == 'LINE']

    # וולידציה וחילוץ נתונים
    is_receipt, data, error_msg = validate_and_extract(lines)

    if not is_receipt:
        return respond(400, {'error': error_msg})

    # שמירה ב-S3 וב-DynamoDB
    receipt_id = str(uuid.uuid4())
    file_name = f"{receipt_id}.jpg"
    s3.put_object(Bucket=BUCKET_NAME, Key=file_name, Body=image_data, ContentType='image/jpeg')

    table = dynamodb.Table(TABLE_NAME)
    table.put_item(
        Item={
            'ReceiptId': receipt_id,
            'UploadDate': datetime.now().isoformat(),
            'MerchantName': data['merchant'],
            'TotalAmount': data['total'],
            'VatAmount': data['vat'],
            'PaymentMethod': data['payment_method'],
            'Category': data['category'],
            'S3ImageUrl': f"https://{BUCKET_NAME}.s3.amazonaws.com/{file_name}",
            'Status': 'PROCESSED'
        }
    )

    return respond(200, {
        'message': 'Success!',
        'receiptId': receipt_id,
        'merchant': data['merchant'],
        'total': data['total'],
        'vat': data['vat'],
        'paymentMethod': data['payment_method'],
        'category': data['category']
    })


def lambda_handler(event, context):
    try:
        method = get_http_method(event)

        # תשובת preflight ל-CORS
        if method == 'OPTIONS':
            return respond(200, {'ok': True})

        # שליפת היסטוריית הקבלות
        if method == 'GET':
            receipts = get_all_receipts()
            return respond(200, {'receipts': receipts, 'count': len(receipts)})

        # ברירת מחדל: העלאת קבלה (POST)
        return handle_upload(event)

    except Exception as e:
        print(f"Error: {str(e)}")
        return respond(500, {'error': str(e)})
