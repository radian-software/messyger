import argparse
import collections
import datetime
import json
import random
import re

import esprima
import requests

## Get the email and password

parser = argparse.ArgumentParser("messyger")
parser.add_argument("-u", "--email", required=True)
parser.add_argument("-p", "--password", required=True)
parser.add_argument("-m", "--message")
parser.add_argument("-r", "--recipient", type=int)
args = parser.parse_args()

## Parse the HTML response

html_resp = requests.get("https://www.messenger.com")
html_resp.raise_for_status()
html_page = html_resp.text

initial_request_id = re.search(
    r'name="initial_request_id" value="([^"]+)"', html_page
).group(1)

lsd = re.search(r'name="lsd" value="([^"]+)"', html_page).group(1)

datr = re.search(r'"_js_datr","([^"]+)"', html_page).group(1)

## Make the login request

login = requests.post(
    "https://www.messenger.com/login/password/",
    cookies={"datr": datr},
    data={
        "lsd": lsd,
        "initial_request_id": initial_request_id,
        "email": args.email,
        "pass": args.password,
    },
    allow_redirects=False,
)
assert login.status_code == 302

## Extract the inbox query parameters

inbox_html_resp = requests.get("https://www.messenger.com", cookies=login.cookies)
inbox_html_resp.raise_for_status()
inbox_html_page = inbox_html_resp.text

dtsg = re.search(r'"DTSGInitialData",\[\],\{"token":"([^"]+)"', inbox_html_page).group(
    1
)

device_id = re.search(r'"deviceId":"([^"]+)"', inbox_html_page).group(1)

schema_version = re.search(r'"schemaVersion":"([0-9]+)"', inbox_html_page).group(1)

script_urls = re.findall(r'"([^"]+rsrc\.php/[^"]+\.js[^"]+)"', inbox_html_page)

scripts = []
for url in script_urls:
    resp = requests.get(url)
    resp.raise_for_status()
    scripts.append(resp.text)

for script in scripts:
    if "LSPlatformGraphQLLightspeedRequestQuery" not in script:
        continue
    doc_id = re.search(
        r'id:"([0-9]+)",metadata:\{\},name:"LSPlatformGraphQLLightspeedRequestQuery"',
        script,
    ).group(1)
    break

if not args.message:

    inbox_resp = requests.post(
        "https://www.messenger.com/api/graphql/",
        cookies=login.cookies,
        data={
            "fb_dtsg": dtsg,
            "doc_id": doc_id,
            "variables": json.dumps(
                {
                    "deviceId": device_id,
                    "requestId": 0,
                    "requestPayload": json.dumps(
                        {
                            "database": 1,
                            "version": schema_version,
                            "sync_params": json.dumps({}),
                        }
                    ),
                    "requestType": 1,
                }
            ),
        },
    )
    inbox_resp.raise_for_status()

    ## Parse the inbox data response

    inbox_json = inbox_resp.json()
    inbox_js = inbox_json["data"]["viewer"]["lightspeed_web_request"]["payload"]

    ast = esprima.parseScript(inbox_js)

    def is_lightspeed_call(node):
        return (
            node.type == "CallExpression"
            and node.callee.type == "MemberExpression"
            and node.callee.object.type == "Identifier"
            and node.callee.object.name == "LS"
            and node.callee.property.type == "Identifier"
            and node.callee.property.name == "sp"
        )

    def parse_argument(node):
        if node.type == "Literal":
            return node.value
        if node.type == "ArrayExpression":
            assert len(node.elements) == 2
            high_bits, low_bits = map(parse_argument, node.elements)
            return (high_bits << 32) + low_bits
        if node.type == "UnaryExpression" and node.prefix and node.operator == "-":
            return -parse_argument(node.argument)

    fn_calls = collections.defaultdict(list)

    def handle_node(node, meta):
        if not is_lightspeed_call(node):
            return

        args = [parse_argument(arg) for arg in node.arguments]
        (fn_name, *fn_args) = args

        fn_calls[fn_name].append(fn_args)

    esprima.parseScript(inbox_js, delegate=handle_node)

    conversations = collections.defaultdict(dict)

    for args in fn_calls["deleteThenInsertThread"]:
        last_sent_ts, last_read_ts, last_msg, *rest = args
        user_id, last_msg_author = [
            arg for arg in rest if isinstance(arg, int) and arg > 1e14
        ]
        conversations[user_id]["unread"] = last_sent_ts != last_read_ts
        conversations[user_id]["last_message"] = last_msg
        conversations[user_id]["last_message_author"] = last_msg_author

    for args in fn_calls["verifyContactRowExists"]:
        user_id, _, _, name, *rest = args
        conversations[user_id]["name"] = name

    print(json.dumps(conversations, indent=2))

else:

    ## Replicate the send-message request

    timestamp = int(datetime.datetime.now().timestamp() * 1000)
    epoch = timestamp << 22
    otid = epoch + random.randrange(2 ** 22)

    send_message_resp = requests.post(
        "https://www.messenger.com/api/graphql/",
        cookies=login.cookies,
        data={
            "fb_dtsg": dtsg,
            "doc_id": doc_id,
            "variables": json.dumps(
                {
                    "deviceId": device_id,
                    "requestId": 0,
                    "requestPayload": json.dumps(
                        {
                            "version_id": str(schema_version),
                            "tasks": [
                                {
                                    "label": "46",
                                    "payload": json.dumps(
                                        {
                                            "thread_id": args.recipient,
                                            "otid": "6870463702739115830",
                                            "source": 0,
                                            "send_type": 1,
                                            "text": args.message,
                                            "initiating_source": 1,
                                        }
                                    ),
                                    "queue_name": str(args.recipient),
                                    "task_id": 0,
                                    "failure_count": None,
                                },
                                {
                                    "label": "21",
                                    "payload": json.dumps(
                                        {
                                            "thread_id": args.recipient,
                                            "last_read_watermark_ts": timestamp,
                                            "sync_group": 1,
                                        }
                                    ),
                                    "queue_name": str(args.recipient),
                                    "task_id": 1,
                                    "failure_count": None,
                                },
                            ],
                            "epoch_id": 6870463702858032000,
                        }
                    ),
                    "requestType": 3,
                }
            ),
        },
    )

    print(send_message_resp.text)
