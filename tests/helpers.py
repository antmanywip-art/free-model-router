from datetime import datetime, timedelta, timezone

MAGI_KEY = "test-magi-" + "m"*40
NARA_KEY = "test-nara-" + "n"*40
UPSTREAM_KEY = "test-upstream-" + "u"*40


def configuration(base):
    now = datetime.now(timezone.utc)
    routes = []
    for name in ["a", "b"]:
        routes.append({"id":name,"provider":name,"model":"mock-"+name,"family":"family-"+name,
          "enabled":True,"capabilities":["text","json","tools","stream"],"context_tokens":32768,"max_output_tokens":8192,
          "free":{"status":"free","kind":"quota","checked_at":(now-timedelta(minutes=1)).isoformat(),
                  "verify_until":(now+timedelta(days=1)).isoformat(),"billing_guard":"billing_disabled",
                  "account_verified":True,"evidence":["https://example.test/mock-pricing"]}})
    raw = {"version":1,"max_parallel":2,"timeout_seconds":3,"max_attempts":2,
      "providers":[{"id":x,"base_url":base+"/"+x+"/v1","key_env":"UPSTREAM_KEY","quota_group":x,"requests_per_minute":100,"requests_per_day":1000} for x in ["a","b"]],
      "models":routes,"aliases":{"general":["a","b"],"magi-a":["a"],"magi-b":["b"],"nara-text":["a","b"]},
      "clients":[{"id":"magi","key_env":"MAGI_KEY","allowed_models":["general","a","b","magi-a","magi-b"],"max_parallel":2},
                 {"id":"nara","key_env":"NARA_KEY","allowed_models":["nara-text"],"max_parallel":1}]}
    return raw, {"MAGI_KEY":MAGI_KEY,"NARA_KEY":NARA_KEY,"UPSTREAM_KEY":UPSTREAM_KEY}


def completion(content='{"ok":true}'):
    return {"choices":[{"message":{"role":"assistant","content":content},"finish_reason":"stop"}],
            "usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}
