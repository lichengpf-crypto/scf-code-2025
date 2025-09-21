# index.py —— 超薄入口：先尝试新路由；没匹配就回退到旧入口
import os, sys
# 兜底引入 lib/（qcloud_cos 等三方库）
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lib"))

from router import route_with_fallback
import index_legacy

# 既有模块
from handlers import student_inbox
from handlers import student
from handlers import teacher
from handlers import text_tools

# 路由表（注意：更长前缀要放前面，避免被短前缀“吃掉”）
ROUTES = [
    # —— 学生端（公共/自检）——
    ("GET",  "/ping",                     student.ping),
    ("GET",  "/cos/info",                 student.cos_info),
    ("GET",  "/cos/test",                 student.cos_test),

    ("POST", "/submissions/create",       student.create_submission),
    ("POST", "/submissions/upload_image", student.upload_images),   # 注意：upload_images（复数）
    ("GET",  "/student/inbox",            student_inbox.list_inbox),
    ("POST", "/score/run",                student.score_run),
    ("GET",  "/results/",                 student.get_result),      # /results/<submission_id>

    # —— 老师端（全部直接指向 teacher 模块）——
    ("POST", "/tts/preview",              teacher.tts_preview),
    ("POST", "/assignments/publish_tts",  teacher.publish_tts),
    ("POST", "/assignments/publish",      teacher.publish),
    
    ("GET",  "/assignments/list",         teacher.list_assignments),
    ("GET",  "/assignments/get/",         teacher.get_assignment),  # /assignments/get/<id>
    ("GET",  "/submissions/list",         teacher.list_submissions),
    ("GET",  "/submissions/get/",         teacher.get_submission),  # /submissions/get/<id>
    # —— 工具类 ——
    ("POST", "/text/check_words",  text_tools.check_words),
    ("POST", "/text/validate",     text_tools.validate),

]

def main_handler(event, context):
    return route_with_fallback(event, context, ROUTES, index_legacy.main_handler)
