import os
import json
import boto3
import traceback
from time import gmtime, strftime
from decimal import Decimal
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import time
from datetime import datetime
from flask_cors import CORS 


# ------------------- Load Env -------------------
env = os.getenv("ENV", "Production")
dotenv_path = f".env.{env}"
if os.path.exists(dotenv_path):
    load_dotenv(dotenv_path)

app = Flask(__name__)
CORS(
    app,
    resources={r"/*": {"origins": r".*"}},   # echo any Origin back
    supports_credentials=True,
    allow_headers="*",
    expose_headers="*",
    methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS"],
    max_age=86400
)


# ------------------- AWS DynamoDB -------------------
aws_access_key = os.getenv("AWS_ACCESS_KEY_ID")
aws_secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
aws_region = os.getenv("AWS_DEFAULT_REGION", "us-west-2")

dynamodb = boto3.resource(
    "dynamodb",
    region_name=aws_region,
    aws_access_key_id=aws_access_key,
    aws_secret_access_key=aws_secret_key
)

lambda_client = boto3.client('lambda',region_name=aws_region,
    aws_access_key_id=aws_access_key,
    aws_secret_access_key=aws_secret_key)  



Grade_and_Subject = dynamodb.Table(os.getenv("GRADE_SUBJECT_TABLE", "Grade_and_Subject"))
Investor = dynamodb.Table(os.getenv("INVESTOR_TABLE", "Investor"))
# ICP = dynamodb.Table(os.getenv("ICP_TABLE", "ICP"))
icp_table = dynamodb.Table(os.getenv("ICP_TABLE", "ICP"))
subject_table = dynamodb.Table(os.getenv("SUBJECT_TABLE", "Grade_and_Subject"))
Question_Prod = dynamodb.Table(os.getenv("QUIZ_TABLE", "Question"))
User_ITP_Prod = dynamodb.Table(os.getenv("USER_ITP_TABLE", "User_Infinite_TestSeries"))


# ------------------- API Endpoints -------------------
url_insert_subject = os.getenv("URL_INSERT_SUBJECT")
url_get_school = os.getenv("URL_GET_SCHOOL")
url_insert_lesson_planner = os.getenv("URL_INSERT_LESSON_PLANNER")
# url_itp_initialize = "https://t9or7o19o8.execute-api.us-west-2.amazonaws.com/itpGenerate/api/initialize" #dev
url_itp_initialize = "https://nycoxziw67.execute-api.us-west-2.amazonaws.com/Production/api/initialize"
url_icp_generate = os.getenv("URL_ICP_GENERATE")

# ------------------- Helper Functions -------------------
def insert_into_school(tenantEmail, grade, section, period, grade_and_subject_ui):
    headers = {
        "x-api-key": os.getenv("SCHOOL_API_KEY"),
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
            payload = json.dumps({
                "name": grade_and_subject_ui,
                "grade": grade,
                "section": section,
                "school_id": school_id,
                "period": period
            })
            requests.post(url_insert_subject, headers=headers, data=payload)
    except Exception as e:
        print(f"Failed to insert into school API: {e}")


# def insert_lesson_planner_payload(lesson_data):
#     headers = {
#         "x-api-key": os.getenv("LESSON_PLANNER_API_KEY"),
#         "Content-Type": "application/json"
#     }
#     try:
#         payload = {"lesson_planner": lesson_data}
#         resp = requests.post(url_insert_lesson_planner, headers=headers, data=json.dumps(payload))
#         print(f"Lesson planner insert response: {resp.text}")
#     except Exception as e:
#         print(f"Failed to insert lesson planner payload: {e}")

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
        resp = Investor.get_item(Key={"email": student_email})
        if "Item" not in resp:
            print(f"Student {student_email} not found in Investor")
            return

        student_item = resp["Item"]
        subject_list = student_item.get("subject_list", [])

        if lesson_uuid not in subject_list:
            subject_list.append(lesson_uuid)
            Investor.update_item(
                Key={"email": student_email},
                UpdateExpression="SET subject_list = :s",
                ExpressionAttributeValues={":s": subject_list}
            )
            print(f"Added {lesson_uuid} to {student_email}'s subject_list")
    except Exception as e:
        print(f"Error updating student {student_email}: {e}")


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


# # -------- ICP Helpers --------
# def store_icp_direct(course_data, email, topic_id):
#     """
#     Save generated ICP course into DynamoDB (ICP_TABLE).
#     """
#     try:
#         def convert_numbers(obj):
#             if isinstance(obj, float):
#                 return Decimal(str(obj))
#             elif isinstance(obj, dict):
#                 return {k: convert_numbers(v) for k, v in obj.items()}
#             elif isinstance(obj, list):
#                 return [convert_numbers(v) for v in obj]
#             return obj

#         icp_item = {
#             "email": email.lower(),
#             "id": topic_id,
#             "course": convert_numbers(course_data["course"])
#         }

#         ICP.put_item(Item=icp_item)
#         print(f"[ICP STORE] Saved ICP for email={email}, id={topic_id}")
#         return {"statusCode": 200, "body": {"message": "Stored in DynamoDB", "id": topic_id, "email": email}}

#     except Exception as e:
#         print(f"[ICP STORE ERROR] {e}")
#         return {"statusCode": 500, "body": {"message": f"Error storing course: {str(e)}"}}


# ------------------- Flask Endpoints -------------------
@app.route("/process_all", methods=["POST"])
def process_all():
    try:
        data = request.json
        subject = data.get("subject", "").strip()
        lesson_data = data["body"]
        lesson_uuid = lesson_data["lesson_planner_UUID"]

        now = strftime("%Y-%m-%d,%H:%M:%S", gmtime())
        tenantEmail = "sierracanyon@edyou.com" #Production Variable change to SC production email
        tenantName = "Sierra Canyon"
        icon = "https://pollydemo2022.s3.us-west-2.amazonaws.com/icons/Geometry.svg"

        grade = lesson_data.get("grade", "")
        section = lesson_data.get("section", "")
        period = lesson_data.get("period", "")

        # Dynamo insert
        item = {
            "id": lesson_uuid,
            "Created_at": now,
            "Grade": grade,
            "Grade_and_Subject": f"TD: {subject}",
            "Grade_and_Subject_UI": subject,
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

        insert_into_school(tenantEmail, grade, section, period, subject)
        insert_lesson_planner_payload(lesson_data)

        for email in lesson_data.get("student", []):
            update_student_subject_list(email, lesson_uuid)

        return jsonify({
            "status": "success",
            "uuid": lesson_uuid,
            "message": f"Subject {subject} inserted successfully, lesson planner stored, uuid assigned to {len(lesson_data.get('student', []))} students"
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e), "trace": traceback.format_exc()}), 400


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



if __name__ == "__main__":
    app.run(debug=True, port=5000)
