"""
Flask App with Integrated AI Question Generation API
"""

import os
import json
import boto3
import traceback
import time
from time import gmtime, strftime
from decimal import Decimal
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
import requests
import logging
from pydantic import BaseModel, Field, ValidationError
from typing import List, Optional
from openai import OpenAI

# ------------------- Logging -------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------- Load Env -------------------
env = os.getenv("ENV", "Production")
dotenv_path = f".env.{env}"
if os.path.exists(dotenv_path):
    load_dotenv(dotenv_path)

# ------------------- Flask App -------------------
app = Flask(__name__)
CORS(
    app,
    resources={r"/*": {"origins": r".*"}},
    supports_credentials=True,
    allow_headers="*",
    expose_headers="*",
    methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS"],
    max_age=86400
)

# ------------------- AWS Clients -------------------
aws_access_key = os.getenv("AWS_ACCESS_KEY_ID")
aws_secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
aws_region = os.getenv("AWS_DEFAULT_REGION", "us-west-2")

dynamodb = boto3.resource(
    "dynamodb",
    region_name=aws_region,
    aws_access_key_id=aws_access_key,
    aws_secret_access_key=aws_secret_key
)

lambda_client = boto3.client(
    "lambda",
    region_name=aws_region,
    aws_access_key_id=aws_access_key,
    aws_secret_access_key=aws_secret_key
)

s3_client = boto3.client(
    "s3",
    region_name=aws_region,
    aws_access_key_id=aws_access_key,
    aws_secret_access_key=aws_secret_key
)

# Dynamo Tables
Grade_and_Subject = dynamodb.Table(os.getenv("GRADE_SUBJECT_TABLE", "Grade_and_Subject"))
Investor = dynamodb.Table(os.getenv("INVESTOR_TABLE", "Investor"))
icp_table = dynamodb.Table(os.getenv("ICP_TABLE", "ICP"))
subject_table = dynamodb.Table(os.getenv("SUBJECT_TABLE", "Grade_and_Subject"))
Question_Prod = dynamodb.Table(os.getenv("QUIZ_TABLE", "Question"))
User_ITP_Prod = dynamodb.Table(os.getenv("USER_ITP_TABLE", "User_Infinite_TestSeries"))

# S3
BUCKET_NAME = "icp-image-gen"

# External URLs
url_insert_subject = os.getenv("URL_INSERT_SUBJECT")
url_get_school = os.getenv("URL_GET_SCHOOL")
url_insert_lesson_planner = os.getenv("URL_INSERT_LESSON_PLANNER")
url_itp_initialize = "https://nycoxziw67.execute-api.us-west-2.amazonaws.com/Production/api/initialize"
url_icp_generate = os.getenv("URL_ICP_GENERATE")

# ------------------- OpenAI Client -------------------
try:
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
except Exception as e:
    logger.error(f"Failed to initialize OpenAI client: {e}")
    raise

# ==================== AI Question Generation Models ====================

class QuizQuestion(BaseModel):
    Question: str
    options__001: str
    description__001: str
    options__002: str
    description__002: str
    options__003: str
    description__003: str
    options__004: str
    description__004: str
    CorrectAnswer: int = Field(..., ge=1, le=4)

class QuizResponse(BaseModel):
    questions: List[QuizQuestion]

class GenerateRequest(BaseModel):
    subject: str
    topic: str
    subtopic: Optional[str] = None
    grade_level: str
    difficulty: str = Field(..., pattern="^(easy|medium|hard)$")
    learning_style: Optional[str] = None
    additional_context: Optional[str] = None

class GenerateResponse(BaseModel):
    success: bool
    question: Optional[QuizQuestion] = None
    error_message: Optional[str] = None

class RegenerateRequest(BaseModel):
    current_question: dict
    edit_instruction: str
    subject: str
    topic: str
    grade_level: str
    difficulty: str = Field(..., pattern="^(easy|medium|hard)$")

class RegenerateResponse(BaseModel):
    success: bool
    regenerated_question: Optional[QuizQuestion] = None
    changes_summary: Optional[List[str]] = None
    error_message: Optional[str] = None

# ==================== Helper Functions ====================

def create_system_prompt():
    return """You are an expert educator creating high-quality quiz questions.

Requirements:
1. Educationally valuable, not rote
2. Clear, age-appropriate language
3. Use LaTeX for math/science
4. Explanations: 2-3 sentences
5. Exactly one correct option
"""

def format_generate_prompt(request: GenerateRequest) -> str:
    prompt = f"""Create a {request.difficulty} quiz question for {request.subject} 
on {request.topic}{f' - {request.subtopic}' if request.subtopic else ''} 
for {request.grade_level} students.

- 4 MCQ options
- Detailed explanations
- Only one correct answer
"""
    if request.learning_style:
        prompt += f"- Adapt for {request.learning_style} learners\n"
    if request.additional_context:
        prompt += f"Additional context: {request.additional_context}\n"
    return prompt

def format_regenerate_prompt(request: RegenerateRequest) -> str:
    current = request.current_question
    correct_answer = current.get('CorrectAnswer', 1)
    return f"""Modify this quiz question with instruction: {request.edit_instruction}
Keep subject={request.subject}, topic={request.topic}, grade={request.grade_level}, difficulty={request.difficulty}.
Ensure all options and explanations are coherent and only one is correct.
"""

def detect_changes(current_question: dict, new_question: QuizQuestion) -> List[str]:
    changes = []
    if current_question.get('Question') != new_question.Question:
        changes.append("Question text updated")
    changes.append("All options and explanations regenerated")
    return changes

# ==================== AI Question Generation Endpoints ====================

@app.route("/api/ai/generate-question", methods=["POST"])
def generate_question():
    try:
        data = request.json
        req = GenerateRequest(**data)

        system_prompt = create_system_prompt()
        user_prompt = format_generate_prompt(req)

        response = client.beta.chat.completions.parse(
            model="gpt-4o-2024-08-06",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            response_format=QuizResponse
        )

        quiz_data = response.choices[0].message.parsed
        question = quiz_data.questions[0]

        return jsonify(GenerateResponse(success=True, question=question).dict()), 200

    except ValidationError as ve:
        return jsonify({"success": False, "error_message": str(ve)}), 400
    except Exception as e:
        logger.error(f"Error generating question: {str(e)}")
        return jsonify({"success": False, "error_message": str(e)}), 500

@app.route("/api/ai/regenerate-question", methods=["POST"])
def regenerate_question():
    try:
        data = request.json
        req = RegenerateRequest(**data)

        system_prompt = create_system_prompt()
        user_prompt = format_regenerate_prompt(req)

        response = client.beta.chat.completions.parse(
            model="gpt-4o-2024-08-06",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            response_format=QuizResponse
        )

        quiz_data = response.choices[0].message.parsed
        question = quiz_data.questions[0]
        changes = detect_changes(req.current_question, question)

        return jsonify(RegenerateResponse(success=True, regenerated_question=question, changes_summary=changes).dict()), 200

    except ValidationError as ve:
        return jsonify({"success": False, "error_message": str(ve)}), 400
    except Exception as e:
        logger.error(f"Error regenerating question: {str(e)}")
        return jsonify({"success": False, "error_message": str(e)}), 500

# ==================== Your Existing Flask Endpoints ====================


# ------------------- Upload File Endpoint -------------------
@app.route("/api/upload-file", methods=["POST"])
def upload_file():
    try:
        if "file" not in request.files:
            return jsonify({"error": "file is required"}), 400

        file = request.files["file"]
        filename = file.filename

        # Key inside your S3 bucket folder
        key = f"teacher_uploaded_images/{int(time.time())}-{filename}"

        # Upload directly to S3
        s3_client.upload_fileobj(
            file,
            BUCKET_NAME,
            key,
            ExtraArgs={"ContentType": file.content_type}
        )

        # Build file URL (public if bucket policy/ACL allows, or serve via CloudFront)
        file_url = f"https://{BUCKET_NAME}.s3.{aws_region}.amazonaws.com/{key}"

        return jsonify({"fileUrl": file_url}), 200

    except Exception as e:
        print("=== [UPLOAD_FILE ERROR] ===")
        print(traceback.format_exc())
        return jsonify({"error": str(e)}), 500


# ------------------- Helper Functions -------------------
def insert_into_school(tenantEmail, grade, section, period, grade_and_subject_ui):
    headers = {
        "x-api-key": os.getenv("LESSON_PLANNER_API_KEY"),
        "Content-Type": "application/json"
    }
    try:
        school_resp = requests.post(
            url_get_school,
            headers=headers,
            data=json.dumps({"email": tenantEmail})
        )
        school_id = None
        if school_resp.text.strip():
            data = school_resp.json()
            if isinstance(data, list) and data:
                school_id = data[0].get("school_id")

        if school_id:
            payload = {
                "name": grade_and_subject_ui,
                "grade": grade,
                "section": section,
                "school_id": school_id,
                "period": period
            }
            resp = requests.post(url_insert_subject, headers=headers, json=payload)
            print(f"[Insert Subject API] status={resp.status_code}, response={resp.text}")

            if resp.status_code == 200 and resp.text.strip():
                try:
                    body = resp.json()
                    return body.get("inserted_subject_id")   # <-- correct key
                except:
                    pass
        return None
    except Exception as e:
        print(f"Failed to insert into school API: {e}")
        return None


def insert_subject_teacher_relation(subject_id, teacher_id):
    """
    Insert subject-teacher relation into Postgres via API Gateway.
    """
    url = "https://48czgcfeuc.execute-api.us-west-2.amazonaws.com/prod/query?query_name=insert_subject_teacher"
    headers = {
        "x-api-key": os.getenv("LESSON_PLANNER_API_KEY", "oxcoUnpFS89Cu43FvFMGa5ZA5C6Ykxd79sXnuJhh"),
        "Content-Type": "application/json"
    }

    payload = {
        "subject_id": str(subject_id),
        "teacher_id": str(teacher_id),
        "role_id": "",
        "school_year": "",
        "school_year_id": ""
    }

    print("[DEBUG] Posting subject-teacher relation:", json.dumps(payload, indent=2))

    resp = requests.post(url, headers=headers, json=payload)
    print(f"[Subject-Teacher API] status={resp.status_code}, response={resp.text}")

    if resp.status_code != 200:
        raise Exception(f"Insert subject-teacher relation failed (HTTP {resp.status_code}): {resp.text}")

    return resp.json() if resp.text.strip() else {}

def insert_lesson_planner_payload(lesson_data):
    """
    Insert lesson planner into Postgres via API Gateway.
    Exact shape expected by prod:
    {
      "lesson_planner": { ... includes lesson_planner_UUID ... }
    }
    """
    url = "https://48czgcfeuc.execute-api.us-west-2.amazonaws.com/prod/insert?query_name=insert_lesson_planner_payload"
    headers = {
        "x-api-key": os.getenv("LESSON_PLANNER_API_KEY", "oxcoUnpFS89Cu43FvFMGa5ZA5C6Ykxd79sXnuJhh"),
        "Content-Type": "application/json",
    }

    # Build EXACT payload (no "params")
    payload = {"lesson_planner": lesson_data}

    # log what we're sending
    print("[DEBUG] Posting (NO params) payload to Postgres API:\n",
          json.dumps(payload, indent=2)[:2000])

    resp = requests.post(url, headers=headers, json=payload)
    print(f"[Postgres API] status={resp.status_code}, response={resp.text}")

    # Basic failure surfacing
    if resp.status_code != 200:
        raise Exception(f"Postgres insert failed (HTTP {resp.status_code}): {resp.text}")

    # API returns 200 with error body in some cases; check that too
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("error"):
            raise Exception(f"Postgres insert failed: {resp.text}")
    except ValueError:
        # Non-JSON success body, assume OK
        pass

def update_student_subject_list(student_email, lesson_uuid):
    try:
        # First attempt: as-is
        resp = Investor.get_item(Key={"email": student_email})
        
        # If not found, retry with lowercase
        if "Item" not in resp and student_email.lower() != student_email:
            resp = Investor.get_item(Key={"email": student_email.lower()})

        if "Item" not in resp:
            print(f"Student {student_email} not found in Investor (even after lowercase check)")
            return

        student_item = resp["Item"]
        subject_list = student_item.get("subject_list", [])

        if lesson_uuid not in subject_list:
            subject_list.append(lesson_uuid)
            Investor.update_item(
                Key={"email": student_item["email"]},  # use the actual stored key
                UpdateExpression="SET subject_list = :s",
                ExpressionAttributeValues={":s": subject_list}
            )
            print(f"Added {lesson_uuid} to {student_item['email']}'s subject_list")
    except Exception as e:
        print(f"Error updating student {student_email}: {e}")

def get_student_id_by_email(email):
    """
    Fetch student_id for a given student email.
    Uses fixed school_id=3.
    """
    url = "https://48czgcfeuc.execute-api.us-west-2.amazonaws.com/prod/query?query_name=get_student_by_email"
    headers = {
        "x-api-key": os.getenv("LESSON_PLANNER_API_KEY", "oxcoUnpFS89Cu43FvFMGa5ZA5C6Ykxd79sXnuJhh"),
        "Content-Type": "application/json"
    }
    payload = {"email": email, "school_id": 3}
    resp = requests.get(url, headers=headers, json=payload)
    print(f"[GET Student] status={resp.status_code}, response={resp.text}")

    if resp.status_code == 200 and resp.text.strip():
        try:
            body = resp.json()
            if isinstance(body, list) and body:
                return body[0].get("student_id")
        except Exception as e:
            print(f"Error parsing student response: {e}")
    return None


def assign_subject_to_student(student_id, subject_id):
    """
    Assign subject to student via API.
    """
    url = "https://48czgcfeuc.execute-api.us-west-2.amazonaws.com/prod/query?query_name=assign_subject_to_student"
    headers = {
        "x-api-key": os.getenv("LESSON_PLANNER_API_KEY", "oxcoUnpFS89Cu43FvFMGa5ZA5C6Ykxd79sXnuJhh"),
        "Content-Type": "application/json"
    }
    payload = {
        "student_id": str(student_id),
        "subject_id": str(subject_id),
        "assigned_level_id": "",
        "is_homeroom": "False",
        "school_year_id": ""
    }
    resp = requests.post(url, headers=headers, json=payload)
    print(f"[Assign Subject] status={resp.status_code}, response={resp.text}")
    return resp.json() if resp.text.strip() else {}

# -------- ITP Helpers --------
def initialize_itp(itp_payload):
    headers = {"Content-Type": "application/json"}
    print("INIT PAYLOAD:", json.dumps(itp_payload, indent=2))
    resp = requests.post(url_itp_initialize, headers=headers, data=json.dumps(itp_payload))
    return resp.text


def check_itp_status_local(itp_id, user_id=None, pre_defined=True):
    try:
        if pre_defined:
            question_item = Question_Prod.get_item(Key={"id": itp_id})
        else:
            question_item = User_ITP_Prod.get_item(Key={"email": user_id, "id": itp_id})

        if "Item" in question_item:
            item = question_item["Item"]
            if item.get("Generated") is True:
                return {
                    "statusCode": 200,
                    "isGenerated": True,
                    "body": {"id": itp_id, "series_title": item.get("series_title")}
                }
            elif item.get("Generated") is False:
                return {
                    "statusCode": 200,
                    "id": itp_id,
                    "isGenerated": False,
                    "title": item.get("series_title")
                }
            else:
                return {"statusCode": 400, "isGenerated": "error"}
        else:
            return {"statusCode": 404, "message": "ITP not found"}
    except Exception as e:
        print("Error checking ITP status:", e)
        return {"statusCode": 500, "error": str(e)}

# ------------------- Flask Endpoints -------------------
@app.route("/process_all", methods=["POST"])
def process_all():
    try:
        data = request.json
        subject = data.get("subject", "").strip()
        lesson_data = data["body"]
        lesson_uuid = lesson_data["lesson_planner_UUID"]

        now = strftime("%Y-%m-%d,%H:%M:%S", gmtime())
        tenantEmail = "sierracanyon@edyou.com"
        tenantName = "Sierra Canyon"
        icon = "https://pollydemo2022.s3.us-west-2.amazonaws.com/icons/homework.png"

        grade = lesson_data.get("grade", "")
        section = lesson_data.get("section", "")
        period = lesson_data.get("period", "")
        teacher_id = lesson_data.get("teacher_id")

        print("=== [PROCESS_ALL START] ===")

        # Step 1: Dynamo insert
        item = {
            "id": lesson_uuid,
            "Created_at": now,
            "Grade": grade,
            "Grade_and_Subject": f"TD: {subject}",
            "Grade_and_Subject_UI": f"{subject} - Assignment",
            "status": "Active",
            "Subject": subject,
            "tenantEmail": tenantEmail,
            "tenantName": tenantName,
            "quiz_credit": Decimal(0),
            "course_credit": Decimal(0),
            "icon": icon,
            "Period": period,
            "Section": section
        }
        Grade_and_Subject.put_item(Item=item)
        print("[STEP 1] Inserted into DynamoDB Grade_and_Subject")

        # Step 2: Insert subject into school API
        subject_id = insert_into_school(tenantEmail, grade, section, period, subject)
        if subject_id:
            print(f"[STEP 2] Inserted into School API, subject_id={subject_id}")
        else:
            print("[STEP 2] Failed to insert into School API or subject_id missing")

        # Step 2.5: Assign subject to students
        assigned = []
        not_found = []
        failed = []

        students = lesson_data.get("student", [])
        if subject_id and students:
            for student_email in students:
                student_id = get_student_id_by_email(student_email)
                if student_id:
                    assign_resp = assign_subject_to_student(student_id, subject_id)
                    if assign_resp.get("status") == "assigned":
                        assigned.append({"email": student_email, "student_id": student_id})
                        print(f"[STEP 2.5] Assigned subject {subject_id} to {student_email} (id={student_id})")
                    else:
                        failed.append({"email": student_email, "student_id": student_id, "resp": assign_resp})
                        print(f"[STEP 2.5] Assignment failed for {student_email}: {assign_resp}")
                else:
                    not_found.append(student_email)
                    print(f"[STEP 2.5] Could not fetch student_id for {student_email}")
        else:
            print("[STEP 2.5] Skipped subject assignment (no subject_id or students)")

        # Step 3: Insert subject-teacher relation
        if subject_id and teacher_id:
            insert_subject_teacher_relation(subject_id, teacher_id)
            print(f"[STEP 3] Inserted subject-teacher relation (subject_id={subject_id}, teacher_id={teacher_id})")
        else:
            print("[STEP 3] Skipped subject-teacher relation (missing subject_id or teacher_id)")

        # Step 4: Insert lesson planner
        insert_lesson_planner_payload(lesson_data)
        print("[STEP 4] Inserted lesson planner into Postgres API")

        print("=== [PROCESS_ALL END SUCCESS] ===")

        return jsonify({
            "status": "success",
            "uuid": lesson_uuid,
            "message": f"Subject {subject} inserted successfully, lesson planner stored.",
            "assigned_students": assigned,
            "not_found_students": not_found,
            "failed_assignments": failed
        }), 200

    except Exception as e:
        print("=== [PROCESS_ALL ERROR] ===")
        print(traceback.format_exc())
        return jsonify({
            "status": "error",
            "error": str(e),
            "trace": traceback.format_exc()
        }), 400

@app.route("/generate_itp", methods=["POST"])
def api_generate_itp():
    try:
        data = request.json
        init_resp = json.loads(initialize_itp(data))

        print("*******************************")
        print(init_resp)
        print(init_resp['statusCode'])
        print("*******************************")

        # Case 1: ITP already generated
        if init_resp['statusCode'] == 400:
            return jsonify({'body': init_resp['body'], 'statusCode': 400}), 400

        # Case 2: Generating → start polling Question_Prod
        if init_resp['statusCode'] == 200 and init_resp['body'].get("generating") is True:
            itp_id = init_resp['body']["id"]
            user_id = data.get("user_id")

            max_attempts = 80   # ~4 minutes if interval=3s
            interval = 3        # seconds

            for attempt in range(max_attempts):
                time.sleep(interval)
                check_resp = check_itp_status_local(itp_id, user_id, pre_defined=True)
                print(f"[POLL LOOP] Attempt {attempt+1}/{max_attempts}: {check_resp}")

                if check_resp.get("isGenerated"):
                    return jsonify({
                        "status": "success",
                        "message": "ITP generated successfully",
                        "data": check_resp
                    }), 200

            # Timed out
            return jsonify({
                "status": "timeout",
                "message": "ITP generation still in progress after 4 minutes",
                "id": itp_id
            }), 202

        # Case 3: Unexpected but OK → just return init response
        if init_resp['statusCode'] == 200:
            return jsonify({'body': init_resp['body'], 'statusCode': 200}), 200

        # Fallback
        return jsonify({
            "status": "error",
            "message": "Unexpected initialize response",
            "response": init_resp
        }), 400

    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e),
            "trace": traceback.format_exc()
        }), 500


@app.route("/generate_icp", methods=["POST"])
def api_generate_icp():
    # try:
        data = request.json
        # print("[ICP] Incoming request:", json.dumps(data, indent=2))

        subject_id = data.get("subject_id")
        topic_id = data.get("topic_id")
        tenantEmail = data.get("tenantEmail")

        # ---------------- Step 3: Call Generate Course API ----------------
        generate_payload = {
            "topic": data["topic"],
            "audience": data["audience"],
            "icp_UUID": data["icp_UUID"],
            "description": data["description"]
        }
        # print("[ICP] Generate payload:", json.dumps(generate_payload, indent=2))

        resp_generate = requests.post(
            url_icp_generate,
            headers={"Content-Type": "application/json"},
            data=json.dumps(generate_payload)
        )

        # print
        print(f"[ICP] Generate API status: {resp_generate.status_code}")
        print(f"[ICP] Generate API raw text (first 500 chars): {resp_generate.text[:500]}")
        
        if resp_generate.status_code == 200:
            resp = json.loads(resp_generate.text)
            payload_1 ={
                "user_id":tenantEmail,
                "body":{   
                "module": "ICP",
                "body": resp["course"],
                "env": "production", #production
                "subject_id": subject_id,
                "topic_id": topic_id
            }}

            invoke_resp = invoke_lambda(payload_1)

            if invoke_resp["statusCode"] == 200:
                return jsonify({
                    "status": invoke_resp["body"]
                }), 200 

            elif invoke_resp["statusCode"] == 400:
                return jsonify({
                    "status": invoke_resp["body"]
                }), 400 



@app.route("/update_student_subjects", methods=["POST"])
def api_update_student_subjects():
    try:
        data = request.json
        body = data.get("body", {})

        lesson_uuid = body.get("lesson_planner_UUID")
        students = body.get("student", [])

        if not lesson_uuid or not students:
            return jsonify({
                "status": "error",
                "message": "body.lesson_planner_UUID and body.student[] are required"
            }), 400

        updated = []
        not_found = []
        already_linked = []

        for email in students:
            resp = Investor.get_item(Key={"email": email})
            if "Item" not in resp and email.lower() != email:
                resp = Investor.get_item(Key={"email": email.lower()})

            if "Item" not in resp:
                not_found.append(email)
                continue

            student_item = resp["Item"]
            subject_list = student_item.get("subject_list", [])

            if lesson_uuid in subject_list:
                already_linked.append(student_item["email"])
            else:
                subject_list.append(lesson_uuid)
                Investor.update_item(
                    Key={"email": student_item["email"]},
                    UpdateExpression="SET subject_list = :s",
                    ExpressionAttributeValues={":s": subject_list}
                )
                updated.append(student_item["email"])

        return jsonify({
            "status": "success",
            "lesson_planner_UUID": lesson_uuid,
            "updated_students": updated,
            "already_linked": already_linked,
            "not_found": not_found
        }), 200

    except Exception as e:
        print("=== [UPDATE_STUDENT_SUBJECTS ERROR] ===")
        print(traceback.format_exc())
        return jsonify({
            "status": "error",
            "error": str(e),
            "trace": traceback.format_exc()
        }), 500



def invoke_lambda(payload):
    # The name of your Lambda function
    function_name = 'createPredefinedModule'

    # Payload received from POST request
    
    alias_name = 'Production' #Production
    # Invoke Lambda
    response = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType='RequestResponse',  # 'Event' for async, 'RequestResponse' for sync
        Payload=json.dumps(payload),
        Qualifier=alias_name
    )

    # Read response from Lambda
    response_payload = response['Payload'].read()
    result = json.loads(response_payload)

    return result




# ==================== Run ====================

if __name__ == "__main__":
    app.run(debug=True, port=5000)
