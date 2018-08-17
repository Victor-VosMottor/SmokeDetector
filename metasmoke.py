# coding=utf-8
import json
import requests
from globalvars import GlobalVars
import threading
# noinspection PyPackageRequirements
import websocket
from collections import Iterable
from datetime import datetime, timedelta
from glob import glob
from regex import sub
import sys
import traceback
import time
import os
import datahandling
import parsing
import apigetpost
import spamhandling
import classes
import chatcommunicate
from helpers import api_parameter_from_link, log, only_blacklists_changed
from gitmanager import GitManager
from blacklists import load_blacklists


# noinspection PyClassHasNoInit,PyBroadException,PyUnresolvedReferences,PyProtectedMember
class Metasmoke:
    @staticmethod
    def init_websocket():
        has_succeeded = False
        while True:
            try:
                GlobalVars.metasmoke_ws = websocket.create_connection(GlobalVars.metasmoke_ws_host,
                                                                      origin=GlobalVars.metasmoke_host)
                payload = json.dumps({"command": "subscribe",
                                      "identifier": "{\"channel\":\"SmokeDetectorChannel\","
                                      "\"key\":\"" + GlobalVars.metasmoke_key + "\"}"})
                GlobalVars.metasmoke_ws.send(payload)

                GlobalVars.metasmoke_ws.settimeout(10)

                has_succeeded = True
                while True:
                    a = GlobalVars.metasmoke_ws.recv()
                    try:
                        data = json.loads(a)
                        GlobalVars.metasmoke_last_ping_time = datetime.now()
                        Metasmoke.handle_websocket_data(data)
                    except Exception as e:
                        GlobalVars.metasmoke_ws = websocket.create_connection(GlobalVars.metasmoke_ws_host,
                                                                              origin=GlobalVars.metasmoke_host)
                        payload = json.dumps({"command": "subscribe",
                                              "identifier": "{\"channel\":\"SmokeDetectorChannel\"}"})
                        GlobalVars.metasmoke_ws.send(payload)
                        log('error', e)
                        traceback.print_exc()
            except websocket.WebSocketException:
                log('error', "Couldn't bind to MS websocket")
                if not has_succeeded:
                    break
                else:
                    time.sleep(10)

    @staticmethod
    def check_last_pingtime():
        now = datetime.utcnow()
        errlog = open('errorLogs.txt', 'a', encoding="utf-8")
        if GlobalVars.metasmoke_last_ping_time is None:
            errlog.write("\nINFO/WARNING: SmokeDetector has not received a ping yet, forcing SmokeDetector restart "
                         "to try and reset the connection states.\n%s UTC\n" % now)
            os._exit(10)
        elif GlobalVars.metasmoke_last_ping_time < (datetime.now() - timedelta(seconds=120)):
            errlog.write("\nWARNING: Last metasmoke ping with a response was over 120 seconds ago, "
                         "forcing SmokeDetector restart to reset all sockets.\n%s UTC\n" % now)
            # os._exit(10)
        else:
            pass  # Do nothing

    @staticmethod
    def handle_websocket_data(data):
        if "message" not in data:
            return

        message = data['message']
        if isinstance(message, Iterable):
            if "message" in message:
                chatcommunicate.tell_rooms_with("metasmoke", message['message'])
            elif "autoflag_fp" in message:
                event = message["autoflag_fp"]

                chatcommunicate.tell_rooms(event["message"], ("debug", "site-" + event["site"]),
                                           ("no-site-" + event["site"],), notify_site="/autoflag_fp")
            elif "exit" in message:
                os._exit(message["exit"])
            elif "blacklist" in message:
                ids = (message['blacklist']['uid'], message['blacklist']['site'])

                datahandling.add_blacklisted_user(ids, "metasmoke", message['blacklist']['post'])
                datahandling.last_feedbacked = (ids, time.time() + 60)
            elif "unblacklist" in message:
                ids = (message['unblacklist']['uid'], message['unblacklist']['site'])
                datahandling.remove_blacklisted_user(ids)
            elif "naa" in message:
                post_site_id = parsing.fetch_post_id_and_site_from_url(message["naa"]["post_link"])
                datahandling.add_ignored_post(post_site_id[0:2])
            elif "fp" in message:
                post_site_id = parsing.fetch_post_id_and_site_from_url(message["fp"]["post_link"])
                datahandling.add_false_positive(post_site_id[0:2])
            elif "report" in message:
                post_data = apigetpost.api_get_post(message["report"]["post_link"])
                if post_data is None or post_data is False:
                    return
                if datahandling.has_already_been_posted(post_data.site, post_data.post_id, post_data.title) \
                        and not datahandling.is_false_positive((post_data.post_id, post_data.site)):
                    return
                user = parsing.get_user_from_url(post_data.owner_url)
                post = classes.Post(api_response=post_data.as_dict)

                scan_spam, scan_reasons, scan_why = spamhandling.check_if_spam(post)
                if scan_spam:
                    why_append = u"This post would have also been caught for: " + \
                        u", ".join(scan_reasons).capitalize() + "\n" + scan_why
                else:
                    why_append = u"This post would not have been caught otherwise."

                # Add user to blacklist *after* post is scanned
                if user is not None:
                    datahandling.add_blacklisted_user(user, "metasmoke", post_data.post_url)

                why = u"Post manually reported by user *{}* from metasmoke.\n\n{}".format(
                    message["report"]["user"], why_append)

                spamhandling.handle_spam(post=post,
                                         reasons=["Manually reported " + post_data.post_type],
                                         why=why)
            elif "deploy_updated" in message:
                sha = message["deploy_updated"]["head_commit"]["id"]
                if sha != os.popen('git log -1 --pretty="%H"').read():
                    if "autopull" in message["deploy_updated"]["head_commit"]["message"]:
                        if only_blacklists_changed(GitManager.get_remote_diff()):
                            commit_md = "[`{0}`](https://github.com/Charcoal-SE/SmokeDetector/commit/{0})" \
                                        .format(sha[:7])
                            i = []  # Currently no issues with backlists
                            for bl_file in glob('bad_*.txt') + glob('blacklisted_*.txt'):  # Check blacklists for issues
                                with open(bl_file, 'r') as lines:
                                    seen = dict()
                                    for lineno, line in enumerate(lines, 1):
                                        if line.endswith('\r\n'):
                                            i.append("DOS line ending at `{0}:{1}` in {2}".format(bl_file, lineno,
                                                                                                  commit_md))
                                        if not line.endswith('\n'):
                                            i.append("No newline at end of `{0}` in {1}".format(bl_file, commit_md))
                                        if line == '\n':
                                            i.append("Blank line at `{0}:{1}` in {2}".format(bl_file, lineno,
                                                                                             commit_md))
                                        if line in seen:
                                            i.append("Duplicate entry of {0} at lines {1} and {2} of {3} in {4}"
                                                     .format(line.rstrip('\n'), seen[line], lineno, bl_file, commit_md))
                                        seen[line] = lineno
                            if i == []:  # No issues
                                GitManager.pull_remote()
                                load_blacklists()
                                chatcommunicate.tell_rooms_with("debug", "No code modified in {0}, only blacklists"
                                                                " reloaded.".format(commit_md))
                            else:
                                i.append("please fix before pulling.")
                                chatcommunicate.tell_rooms_with("debug", ", ".join(i))
            elif "commit_status" in message:
                c = message["commit_status"]
                sha = c["commit_sha"][:7]
                if c["commit_sha"] != os.popen('git log -1 --pretty="%H"').read():
                    if c["status"] == "success":
                        if "autopull" in c["commit_message"]:
                            s = "[CI]({ci_link}) on [`{commit_sha}`](https://github.com/Charcoal-SE/SmokeDetector/" \
                                "commit/{commit_sha})"\
                                " succeeded. Message contains 'autopull', pulling...".format(ci_link=c["ci_url"],
                                                                                             commit_sha=sha)
                            chatcommunicate.tell_rooms_with("debug", s, notify_site="/ci")
                            time.sleep(2)
                            os._exit(3)
                        else:
                            s = "[CI]({ci_link}) on [`{commit_sha}`](https://github.com/Charcoal-SE/SmokeDetector/" \
                                "commit/{commit_sha}) succeeded.".format(ci_link=c["ci_url"], commit_sha=sha)

                            chatcommunicate.tell_rooms_with("debug", s, notify_site="/ci")
                    elif c["status"] == "failure":
                        s = "[CI]({ci_link}) on [`{commit_sha}`](https://github.com/Charcoal-SE/SmokeDetector/" \
                            "commit/{commit_sha}) failed.".format(ci_link=c["ci_url"], commit_sha=sha)

                        chatcommunicate.tell_rooms_with("debug", s, notify_site="/ci")
            elif "everything_is_broken" in message:
                if message["everything_is_broken"] is True:
                    os._exit(6)

    @staticmethod
    def send_stats_on_post(title, link, reasons, body, username, user_link, why, owner_rep,
                           post_score, up_vote_count, down_vote_count):
        if GlobalVars.metasmoke_host is None:
            log('info', 'Attempted to send stats but metasmoke_host is undefined. Ignoring.')
            return

        metasmoke_key = GlobalVars.metasmoke_key

        try:
            if len(why) > 2048:
                why = why[:1024] + '...' + why[-1021:]  # Basic maths

            post = {'title': title, 'link': link, 'reasons': reasons,
                    'body': body, 'username': username, 'user_link': user_link,
                    'why': why, 'user_reputation': owner_rep, 'score': post_score,
                    'upvote_count': up_vote_count, 'downvote_count': down_vote_count}

            # Remove None values (if they somehow manage to get through)
            post = {k: v for k, v in post.items() if v}

            payload = {'post': post, 'key': metasmoke_key}
            headers = {'Content-type': 'application/json'}
            requests.post(GlobalVars.metasmoke_host + "/posts.json", data=json.dumps(payload), headers=headers)
        except Exception as e:
            log('error', e)

    @staticmethod
    def send_feedback_for_post(post_link, feedback_type, user_name, user_id, chat_host):
        if GlobalVars.metasmoke_host is None:
            log('info', 'Received chat feedback but metasmoke_host is undefined. Ignoring.')
            return

        metasmoke_key = GlobalVars.metasmoke_key

        try:
            payload = {
                'feedback': {
                    'user_name': user_name,
                    'chat_user_id': user_id,
                    'chat_host': chat_host,
                    'feedback_type': feedback_type,
                    'post_link': post_link
                },
                'key': metasmoke_key
            }

            headers = {'Content-type': 'application/json'}
            requests.post(GlobalVars.metasmoke_host + "/feedbacks.json", data=json.dumps(payload), headers=headers)

        except Exception as e:
            log('error', e)

    @staticmethod
    def send_deletion_stats_for_post(post_link, is_deleted):
        if GlobalVars.metasmoke_host is None:
            log('info', 'Attempted to send deletion data but metasmoke_host is undefined. Ignoring.')
            return

        metasmoke_key = GlobalVars.metasmoke_key

        try:
            payload = {
                'deletion_log': {
                    'is_deleted': is_deleted,
                    'post_link': post_link
                },
                'key': metasmoke_key
            }

            headers = {'Content-type': 'application/json'}
            requests.post(GlobalVars.metasmoke_host + "/deletion_logs.json", data=json.dumps(payload), headers=headers)
        except Exception as e:
            log('error', e)

    @staticmethod
    def send_status_ping():
        if GlobalVars.metasmoke_host is None:
            log('info', 'Attempted to send status ping but metasmoke_host is undefined. Not sent.')
            return

        metasmoke_key = GlobalVars.metasmoke_key

        try:
            payload = {
                'location': GlobalVars.location,
                'key': metasmoke_key,
                'standby': GlobalVars.standby_mode
            }

            headers = {'content-type': 'application/json'}
            response = requests.post(GlobalVars.metasmoke_host + "/status-update.json",
                                     data=json.dumps(payload), headers=headers)

            try:
                response = response.json()

                if 'failover' in response and GlobalVars.standby_mode:
                    if response['failover']:
                        GlobalVars.standby_mode = False
                        GlobalVars.metasmoke_last_ping_time = datetime.now()  # Otherwise the ping watcher will exit(10)

                        chatcommunicate.tell_rooms_with("debug", GlobalVars.location + " received failover signal.",
                                                        notify_site="/failover")

                    if response['standby']:
                        chatcommunicate.tell_rooms_with("debug",
                                                        GlobalVars.location + " entering metasmoke-forced standby.")
                        time.sleep(2)
                        os._exit(7)

                if 'shutdown' in response:
                    if response['shutdown']:
                        os._exit(6)

            except Exception:  # TODO: What could happen here?
                pass

        except Exception as e:
            log('error', e)

    @staticmethod
    def update_code_privileged_users_list():
        payload = {'key': GlobalVars.metasmoke_key}
        headers = {'Content-type': 'application/json'}
        response = requests.get(GlobalVars.metasmoke_host + "/api/users/code_privileged",
                                data=json.dumps(payload), headers=headers).json()['items']

        GlobalVars.code_privileged_users = set()

        for id in response["stackexchange_chat_ids"]:
            GlobalVars.code_privileged_users.add(("stackexchange.com", id))

        for id in response["meta_stackexchange_chat_ids"]:
            GlobalVars.code_privileged_users.add(("meta.stackexchange.com", id))

        for id in response["stackoverflow_chat_ids"]:
            GlobalVars.code_privileged_users.add(("stackoverflow.com", id))

    @staticmethod
    def determine_if_autoflagged(post_url):
        """
        Given the URL for a post, determine whether or not it has been autoflagged.
        """
        payload = {
            'key': GlobalVars.metasmoke_key,
            'filter': 'GKNJKLILHNFMJLFKINGJJHJOLGFHJF',  # id and autoflagged
            'urls': post_url
        }
        response = requests.get(GlobalVars.metasmoke_host + "/api/v2.0/posts/urls", params=payload).json()

        if len(response["items"]) > 0 and response["items"][0]["autoflagged"]:
            # get flagger names
            id = str(response["items"][0]["id"])
            payload = {'key': GlobalVars.metasmoke_key}

            flags = requests.get(GlobalVars.metasmoke_host + "/api/v2.0/posts/" + id + "/flags", params=payload).json()

            if len(flags["items"]) > 0:
                return True, [user["username"] for user in flags["items"][0]["autoflagged"]["users"]]

        return False, []

    @staticmethod
    def stop_autoflagging():
        payload = {'key': GlobalVars.metasmoke_key}
        headers = {'Content-type': 'application/json'}

        requests.post(GlobalVars.metasmoke_host + "/flagging/smokey_disable",
                      data=json.dumps(payload), headers=headers)

    @staticmethod
    def send_statistics():
        GlobalVars.posts_scan_stats_lock.acquire()
        if GlobalVars.post_scan_time != 0:
            posts_per_second = GlobalVars.num_posts_scanned / GlobalVars.post_scan_time
            payload = {'key': GlobalVars.metasmoke_key,
                       'statistic': {'posts_scanned': GlobalVars.num_posts_scanned, 'api_quota': GlobalVars.apiquota,
                                     'post_scan_rate': posts_per_second}}
        else:
            payload = {'key': GlobalVars.metasmoke_key,
                       'statistic': {'posts_scanned': GlobalVars.num_posts_scanned, 'api_quota': GlobalVars.apiquota}}

        GlobalVars.post_scan_time = 0
        GlobalVars.num_posts_scanned = 0
        GlobalVars.posts_scan_stats_lock.release()

        headers = {'Content-type': 'application/json'}

        if GlobalVars.metasmoke_host is not None:
            log('info', 'Sent statistics to metasmoke: ', payload['statistic'])
            requests.post(GlobalVars.metasmoke_host + "/statistics.json",
                          data=json.dumps(payload), headers=headers)

    @staticmethod
    def post_auto_comment(msg, user, url=None, ids=None):
        if not GlobalVars.metasmoke_key:
            log('info', 'Ignoring auto-comment')
            return

        response = None

        if url is not None:
            params = {"key": GlobalVars.metasmoke_key, "urls": url, "filter": "GFGJGHFJNFGNHKNIKHGGOMILHKLJIFFN"}
            response = requests.get(GlobalVars.metasmoke_host + "/api/v2.0/posts/urls", params=params).json()
        elif ids is not None:
            post_id, site = ids
            site = api_parameter_from_link(site)
            params = {"key": GlobalVars.metasmoke_key, "filter": "GFGJGHFJNFGNHKNIKHGGOMILHKLJIFFN"}

            response = requests.get("{}/api/v2.0/posts/uid/{}/{}".format(GlobalVars.metasmoke_host,
                                                                         site,
                                                                         post_id), params=params).json()

        if response and "items" in response and len(response["items"]) > 0:
            ms_id = response["items"][0]["id"]
            params = {"key": GlobalVars.metasmoke_key,
                      "text": msg,
                      "chat_user_id": user.id,
                      "chat_host": user._client.host}

            requests.post("{}/api/v2.0/comments/post/{}".format(GlobalVars.metasmoke_host, ms_id), params=params)

    @staticmethod
    def get_post_bodies_from_ms(post_url):
        if not GlobalVars.metasmoke_key:
            return None

        payload = {
            'key': GlobalVars.metasmoke_key,
            'filter': 'HNKHHGINKFKGIKGLGKIILMKNHHGHFOL',  # posts.body, posts.created_at
            'urls': parsing.to_protocol_relative(post_url)
        }
        response = requests.get(GlobalVars.metasmoke_host + '/api/v2.0/posts/urls', params=payload).json()

        return response['items']
