"""
Vercel Serverless Function: /api/quests
CRUD operations for the Quest Configuration Dashboard.
Requires Authorization header using ADMIN_PASSCODE env var.
"""

from http.server import BaseHTTPRequestHandler
import json
import pymysql
import pymysql.cursors
import os

TIER_REWARDS = {
    "common": 50,
    "uncommon": 150,
    "rare": 500,
}
QUEST_TIERS = ["common", "uncommon", "rare"]
QUEST_TASK_TYPES = ["message_count", "vc_minutes", "reaction_count"]

class handler(BaseHTTPRequestHandler):
    
    def _send_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.send_header('Content-type', 'application/json')

    def _is_authenticated(self):
        """Check the Authorization: Bearer <passcode> header against Vercel env var."""
        expected_passcode = os.environ.get('ADMIN_PASSCODE')
        if not expected_passcode:
            return False # Secure by default if not configured
            
        auth_header = self.headers.get('Authorization')
        if not auth_header:
            return False
            
        parts = auth_header.split(" ")
        if len(parts) == 2 and parts[0] == "Bearer":
            return parts[1] == expected_passcode
        return False

    def _get_db_connection(self):
        db_port = os.environ.get('DB_PORT', '3306')
        try:
            db_port_int = int(db_port)
        except ValueError:
            db_port_int = 3306

        return pymysql.connect(
            host=os.environ.get('DB_HOST', ''),
            port=db_port_int,
            user=os.environ.get('DB_USER', ''),
            password=os.environ.get('DB_PASSWORD', ''),
            database=os.environ.get('DB_NAME', ''),
            cursorclass=pymysql.cursors.DictCursor
        )

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        if not self._is_authenticated():
            self.send_response(401)
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({"success": False, "error": "Unauthorized"}).encode('utf-8'))
            return

        self.send_response(200)
        self._send_cors_headers()
        self.end_headers()
        
        # Check if this is a verify request from the dashboard easter egg
        if "?action=verify" in self.path:
            self.wfile.write(json.dumps({"success": True}).encode('utf-8'))
            return

        try:
            connection = self._get_db_connection()
            with connection.cursor() as cursor:
                cursor.execute('''
                    SELECT id, name, description, tier, task_type, target_goal, is_active, created_by, created_at 
                    FROM quests 
                    ORDER BY FIELD(tier, 'common', 'uncommon', 'rare'), name
                ''')
                rows = cursor.fetchall()
            connection.close()

            response = {
                "success": True, 
                "data": rows,
                "tier_rewards": TIER_REWARDS
            }
            self.wfile.write(json.dumps(response, default=str).encode('utf-8'))

        except Exception as e:
            error_response = {"success": False, "error": str(e)}
            self.wfile.write(json.dumps(error_response).encode('utf-8'))

    def do_POST(self):
        if not self._is_authenticated():
            self.send_response(401)
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({"success": False, "error": "Unauthorized"}).encode('utf-8'))
            return

        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)
        
        try:
            data = json.loads(post_data.decode('utf-8'))
            name = data.get('name', '').strip()
            description = data.get('description', '').strip()
            tier = data.get('tier')
            task_type = data.get('task_type')
            target_goal = data.get('target_goal')

            # Validation
            if not name or len(name) > 100:
                raise ValueError("Name must be between 1 and 100 characters.")
            if tier not in QUEST_TIERS:
                raise ValueError(f"Invalid tier. Must be one of: {QUEST_TIERS}")
            if task_type not in QUEST_TASK_TYPES:
                raise ValueError(f"Invalid task_type. Must be one of: {QUEST_TASK_TYPES}")
            try:
                target_goal = int(target_goal)
                if target_goal <= 0: raise ValueError
            except:
                raise ValueError("Target goal must be a positive integer.")

            connection = self._get_db_connection()
            with connection.cursor() as cursor:
                cursor.execute('''
                    INSERT INTO quests (name, description, tier, task_type, target_goal, created_by)
                    VALUES (%s, %s, %s, %s, %s, 0)
                ''', (name, description, tier, task_type, target_goal))
                insert_id = cursor.lastrowid
            connection.commit()
            connection.close()
            
            self.send_response(200)
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "id": insert_id}).encode('utf-8'))

        except Exception as e:
            self.send_response(400)
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode('utf-8'))

    def do_PUT(self):
        if not self._is_authenticated():
            self.send_response(401)
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({"success": False, "error": "Unauthorized"}).encode('utf-8'))
            return

        content_length = int(self.headers.get('Content-Length', 0))
        put_data = self.rfile.read(content_length)
        
        try:
            data = json.loads(put_data.decode('utf-8'))
            quest_id = data.get('id')
            if not quest_id:
                raise ValueError("Quest ID is required.")

            allowed_fields = {"name", "description", "tier", "task_type", "target_goal", "is_active"}
            updates = {k: v for k, v in data.items() if k in allowed_fields}
            
            if not updates:
                raise ValueError("No fields to update.")

            # Validation
            if "name" in updates:
                updates["name"] = updates["name"].strip()
                if not updates["name"] or len(updates["name"]) > 100:
                    raise ValueError("Name must be between 1 and 100 characters.")
            if "description" in updates:
                updates["description"] = updates["description"].strip()
            if "tier" in updates and updates["tier"] not in QUEST_TIERS:
                raise ValueError(f"Invalid tier. Must be one of: {QUEST_TIERS}")
            if "task_type" in updates and updates["task_type"] not in QUEST_TASK_TYPES:
                raise ValueError(f"Invalid task_type. Must be one of: {QUEST_TASK_TYPES}")
            if "target_goal" in updates:
                try:
                    updates["target_goal"] = int(updates["target_goal"])
                    if updates["target_goal"] <= 0: raise ValueError
                except:
                    raise ValueError("Target goal must be a positive integer.")
            if "is_active" in updates:
                updates["is_active"] = bool(updates["is_active"])

            set_clauses = ", ".join(f"{k} = %s" for k in updates)
            params = list(updates.values()) + [quest_id]

            connection = self._get_db_connection()
            with connection.cursor() as cursor:
                cursor.execute(f"UPDATE quests SET {set_clauses} WHERE id = %s", tuple(params))
            connection.commit()
            connection.close()
            
            self.send_response(200)
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode('utf-8'))

        except Exception as e:
            self.send_response(400)
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode('utf-8'))

    def do_DELETE(self):
        if not self._is_authenticated():
            self.send_response(401)
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({"success": False, "error": "Unauthorized"}).encode('utf-8'))
            return

        # Parsing ID from url params or body. Let's support both, or just body.
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length > 0:
            delete_data = self.rfile.read(content_length)
            data = json.loads(delete_data.decode('utf-8'))
            quest_id = data.get('id')
            confirm = data.get('confirm', False)
        else:
            quest_id = None
            confirm = False
            
        try:
            if not quest_id:
                raise ValueError("Quest ID is required.")

            connection = self._get_db_connection()
            with connection.cursor() as cursor:
                if not confirm:
                    # Just checking how many progress records would be deleted
                    cursor.execute("SELECT COUNT(*) as count FROM quest_progress WHERE quest_id = %s", (quest_id,))
                    row = cursor.fetchone()
                    progress_count = row['count'] if row else 0
                    connection.close()
                    
                    self.send_response(200)
                    self._send_cors_headers()
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "success": True, 
                        "needs_confirmation": True,
                        "progress_count": progress_count
                    }).encode('utf-8'))
                    return
                
                # Actual deletion
                cursor.execute("DELETE FROM quests WHERE id = %s", (quest_id,))
            connection.commit()
            connection.close()

            self.send_response(200)
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode('utf-8'))

        except Exception as e:
            self.send_response(400)
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode('utf-8'))
