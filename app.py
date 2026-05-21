from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from pymongo import MongoClient
from bson import ObjectId
from bson.errors import InvalidId
import bcrypt
import os
import io
import re
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import gridfs
from flask_jwt_extended import jwt_required, get_jwt_identity, JWTManager, create_access_token
from werkzeug.utils import secure_filename
from apscheduler.schedulers.background import BackgroundScheduler
import smtplib
from email.mime.text import MIMEText

import pytesseract
# Render is Linux - no Windows path needed
# pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
from PIL import Image
import fitz

load_dotenv()

app = Flask(__name__)

# CORS setup
CORS(app, resources={r"/api/*": {"origins": "*", "supports_credentials": True}})

app.config['JWT_SECRET_KEY'] = os.getenv('JWT_SECRET', 'super-secret-key-change-this-min-32-chars')
jwt = JWTManager(app)

client = MongoClient(os.getenv('MONGODB_URI'))
db = client.anjana
users_collection = db.users
documents_collection = db.documents
fs = gridfs.GridFS(db)

ALLOWED_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def extract_expiry_and_type(ocr_text):
    if not ocr_text or ocr_text.startswith("OCR failed"):
        return None, 'Other'

    print("[OCR TEXT]", ocr_text)

    date_patterns = [
        r'EXP\s*:?\s*(\d{2}/\d{2}/\d{4})',
        r'expir(?:y|ation)\s*:\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{4})',
        r'valid\s+till\s*:?\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{4})',
        r'(\d{2}/\d{2}/\d{4})',
        r'(\d{4}-\d{2})'
    ]

    expiry_date = None
    for pattern in date_patterns:
        match = re.search(pattern, ocr_text, re.IGNORECASE)
        if match:
            date_str = match.group(1)
            for fmt in ['%d/%m/%Y', '%m/%d/%Y', '%Y-%m-%d', '%d-%m-%Y']:
                try:
                    expiry_date = datetime.strptime(date_str, fmt)
                    break
                except:
                    continue
            if expiry_date:
                break

    ocr_lower = ocr_text.lower()
    if 'driver license' in ocr_lower or 'driving licence' in ocr_lower or 'driving license' in ocr_lower or 'driver' in ocr_lower:
        doc_type = 'Driving License'
    elif 'aadhar' in ocr_lower or 'aadhaar' in ocr_lower:
        doc_type = 'Aadhar'
    elif 'pan' in ocr_lower:
        doc_type = 'PAN'
    elif 'passport' in ocr_lower:
        doc_type = 'Passport'
    elif 'voter' in ocr_lower:
        doc_type = 'Voter ID'
    else:
        doc_type = 'Other'

    return expiry_date, doc_type

@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    email = data.get('email')
    password = data.get('password')
    name = data.get('name', '')

    if not email or not password:
        return jsonify({'error': 'Email and password required'}), 400

    if users_collection.find_one({'email': email}):
        return jsonify({'error': 'User already exists'}), 400

    hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
    users_collection.insert_one({
        'name': name,
        'email': email,
        'password': hashed,
        'created_at': datetime.now(timezone.utc)
    })
    return jsonify({'message': 'User created'}), 201

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    email = data.get('email')
    password = data.get('password')

    user = users_collection.find_one({'email': email})
    if not user or not bcrypt.checkpw(password.encode('utf-8'), user['password']):
        return jsonify({'error': 'Invalid credentials'}), 401

    access_token = create_access_token(identity=email, expires_delta=timedelta(days=7))
    return jsonify({'token': access_token, 'email': email}), 200

@app.route('/api/profile', methods=['GET'])
@jwt_required()
def profile():
    current_user_email = get_jwt_identity()
    user = users_collection.find_one({'email': current_user_email})
    if not user:
        return jsonify({'error': 'User not found'}), 404
    return jsonify({'email': user['email'], 'name': user.get('name', '')}), 200

@app.route('/api/upload', methods=['POST'])
@jwt_required()
def upload_file():
    current_user_email = get_jwt_identity()

    if 'file' not in request.files:
        return jsonify({'msg': 'No file part'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'msg': 'No selected file'}), 400

    if not allowed_file(file.filename):
        return jsonify({'msg': 'File type not allowed'}), 400

    filename = secure_filename(file.filename)
    file_bytes = file.read()

    file_id = fs.put(
        io.BytesIO(file_bytes),
        filename=filename,
        content_type=file.content_type,
        metadata={'user_email': current_user_email}
    )

    extracted_text = ""
    try:
        if filename.lower().endswith('.pdf'):
            doc_pdf = fitz.open(stream=file_bytes, filetype="pdf")
            for page in doc_pdf:
                pix = page.get_pixmap(dpi=300)
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                extracted_text += pytesseract.image_to_string(img) + "\n\n"
            doc_pdf.close()
        elif filename.lower().endswith(('.png', '.jpg', '.jpeg')):
            img = Image.open(io.BytesIO(file_bytes))
            extracted_text = pytesseract.image_to_string(img)
    except Exception as e:
        print(f"[OCR ERROR]: {e}")
        extracted_text = f"OCR failed: {str(e)}"

    extracted_text = extracted_text.strip()
    expiry_date, doc_type = extract_expiry_and_type(extracted_text)

    documents_collection.insert_one({
        'filename': filename,
        'file_id': file_id,
        'user_email': current_user_email,
        'upload_date': datetime.now(timezone.utc),
        'content_type': file.content_type,
        'ocr_text': extracted_text,
        'expiry_date': expiry_date,
        'document_type': doc_type
    })

    return jsonify({
        'msg': 'File uploaded',
        'filename': filename,
        'file_id': str(file_id),
        'ocr_preview': extracted_text[:200],
        'document_type': doc_type,
        'expiry_date': str(expiry_date) if expiry_date else None
    }), 201

@app.route('/api/documents', methods=['GET'])
@jwt_required()
def get_documents():
    current_user_email = get_jwt_identity()
    files = fs.find({'metadata.user_email': current_user_email}).sort('upload_date', -1)

    docs = []
    for file in files:
        meta = documents_collection.find_one({'file_id': file._id})
        docs.append({
            'file_id': str(file._id),
            'filename': file.filename,
            'upload_date': file.upload_date.isoformat(),
            'length': file.length,
            'content_type': file.content_type,
            'ocr_text': meta.get('ocr_text', '') if meta else '',
            'expiry_date': meta.get('expiry_date').isoformat() if meta and meta.get('expiry_date') else None,
            'document_type': meta.get('document_type', 'Other') if meta else 'Other'
        })
    return jsonify(docs), 200

@app.route('/api/documents/search', methods=['GET'])
@jwt_required()
def search_documents():
    current_user_email = get_jwt_identity()
    query = request.args.get('q', '').strip()

    if not query:
        return jsonify([]), 200

    regex = {'$regex': query, '$options': 'i'}
    docs = documents_collection.find({
        'user_email': current_user_email,
        '$or': [
            {'ocr_text': regex},
            {'filename': regex},
            {'document_type': regex}
        ]
    }).sort('upload_date', -1)

    results = []
    for d in docs:
        results.append({
            'file_id': str(d['file_id']),
            'filename': d['filename'],
            'document_type': d.get('document_type', 'Other'),
            'expiry_date': d['expiry_date'].isoformat() if d.get('expiry_date') else None,
            'ocr_preview': d.get('ocr_text', '')[:150]
        })
    return jsonify(results), 200

@app.route('/api/documents/expiring', methods=['GET'])
@jwt_required()
def get_expiring_documents():
    current_user_email = get_jwt_identity()
    days = int(request.args.get('days', 30))

    now = datetime.now(timezone.utc)
    future = now + timedelta(days=days)

    docs = documents_collection.find({
        'user_email': current_user_email,
        'expiry_date': {'$gte': now, '$lte': future, '$ne': None}
    }).sort('expiry_date', 1)

    return jsonify([{
        'file_id': str(d['file_id']),
        'filename': d['filename'],
        'document_type': d.get('document_type', 'Other'),
        'expiry_date': d['expiry_date'].isoformat(),
        'days_left': (d['expiry_date'] - now).days
    } for d in docs]), 200

@app.route('/api/download/<file_id>', methods=['GET'])
@jwt_required()
def download_file(file_id):
    current_user_email = get_jwt_identity()
    doc = documents_collection.find_one({'file_id': ObjectId(file_id), 'user_email': current_user_email})
    if not doc:
        return jsonify({'error': 'File not found'}), 404

    file = fs.get(ObjectId(file_id))
    return send_file(
        io.BytesIO(file.read()),
        download_name=doc['filename'],
        mimetype=doc['content_type'],
        as_attachment=True
    )

@app.route('/api/delete/<file_id>', methods=['DELETE'])
@jwt_required()
def delete_file(file_id):
    current_user_email = get_jwt_identity()
    print(f"[DELETE] Request from {current_user_email} for file_id: {file_id}")

    try:
        obj_id = ObjectId(file_id)
    except InvalidId:
        return jsonify({'error': 'Invalid file_id format'}), 400

    doc = documents_collection.find_one({'file_id': obj_id, 'user_email': current_user_email})
    if not doc:
        return jsonify({'error': 'File not found'}), 404

    try:
        fs.delete(obj_id)
        documents_collection.delete_one({'_id': doc['_id']})
        return jsonify({'message': 'File deleted'}), 200
    except Exception as e:
        print(f"[DELETE] Error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/verify', methods=['GET'])
@jwt_required()
def verify():
    current_user_email = get_jwt_identity()
    return jsonify({'valid': True, 'email': current_user_email}), 200

@app.route('/api/ocr/<file_id>', methods=['GET'])
@jwt_required()
def ocr_file(file_id):
    current_user_email = get_jwt_identity()
    doc = documents_collection.find_one({'file_id': ObjectId(file_id), 'user_email': current_user_email})
    if not doc:
        return jsonify({'error': 'File not found'}), 404

    return jsonify({
        'file_id': file_id,
        'filename': doc['filename'],
        'text': doc.get('ocr_text', ''),
        'document_type': doc.get('document_type', 'Other'),
        'expiry_date': doc.get('expiry_date').isoformat() if doc.get('expiry_date') else None
    })

# ANJANA: OFFLINE NOTIFICATION CODE STARTS HERE
def send_expiry_email(to_email, docs):
    sender = os.getenv('EMAIL_USER')
    password = os.getenv('EMAIL_PASS')

    if not sender or not password:
        print("[EMAIL] Skipped: EMAIL_USER/PASS not set in.env")
        return

    subject = f"DocWallet AI: {len(docs)} document(s) expiring soon"
    body = "Hi,\n\nThe following documents will expire soon:\n\n"
    for d in docs:
        days = (d['expiry_date'] - datetime.now(timezone.utc)).days
        body += f"- {d['filename']} | {d['document_type']} | Expires: {d['expiry_date'].strftime('%d/%m/%Y')} | {days} days left\n"
    body += "\nLogin to DocWallet AI to renew or download."

    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = sender
    msg['To'] = to_email

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(sender, password)
            server.sendmail(sender, to_email, msg.as_string())
        print(f"[EMAIL] Sent to {to_email}")
    except Exception as e:
        print(f"[EMAIL ERROR] {e}")

def check_expiry_and_notify():
    print("[JOB] Running expiry check...")
    now = datetime.now(timezone.utc)
    future = now + timedelta(days=7)

    users = users_collection.distinct('email')
    for email in users:
        docs = list(documents_collection.find({
            'user_email': email,
            'expiry_date': {'$gte': now, '$lte': future, '$ne': None}
        }))
        if docs:
            send_expiry_email(email, docs)

scheduler = BackgroundScheduler()
scheduler.add_job(check_expiry_and_notify, 'interval', days=1, id='expiry_check')
scheduler.start()

@app.teardown_appcontext
def shutdown_scheduler(exception=None):
    if scheduler.running:
        scheduler.shutdown()
# ANJANA: OFFLINE NOTIFICATION CODE ENDS HERE

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', debug=False, port=port, use_reloader=False)