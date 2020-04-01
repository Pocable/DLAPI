# Api Polling Imports
import json
import requests
import time
import myjdapi

# Flask Imports
import flask
from flask import request, jsonify
from flask_apscheduler import APScheduler

# Threading and system imports
import threading
import sys
import os

###########################
#   Configuration Items   #
###########################

# https://my.jdownloader.org/
JDOWNLOADER_USER = os.environ['JD_USER']
JDOWNLOADER_PASS = os.environ['JD_PASS']
JDOWNLOADER_DEVICE = os.environ['JD_DEVICE']

# https://real-debrid.com/apitoken
REAL_DB_KEY = os.environ['RD_KEY']

# Rate at which RD is polled for downloads. Keep > 250
# RD will not finish a torrent under 2.5 minutes and
# I do not want to poll their servers too much.
rate_delay = 150

# Should not be changed
REAL_DB_SERVER = "https://api.real-debrid.com/rest/1.0/"
header = {'Authorization': 'Bearer ' + REAL_DB_KEY }

API_KEY = os.environ['API_KEY']

###########################
# End Configuration Items #
###########################

# Internal global items and flask configuration
watched_content = {}
app = flask.Flask(__name__)
app.config["DEBUG"] = False
device = None

# Threading Configuration
rd_thread = None

# Configuration object for scheduling update
class Config(object):
    JOBS = [
        {
            'id': 'RDListener',
            'func': __name__ + ':rd_listener',
            'args': (),
            'trigger': 'interval',
            'seconds': rate_delay
        }
    ]

    SCHEDULER_API_ENABLED = True

"""
Get the real debrid download url from the website
ident: The identifier for the show your looking for.
returns: The links associated with the identifier
"""
def get_rd_download_urls(ident):
    req = requests.get(REAL_DB_SERVER + "torrents/info/%s" %  ident, headers=header)
    if(req.status_code == 401 or req.status_code == 403):
        raise Exception("Failed to get torrent info. Status code: " + str(req.status_code))
    res = json.loads(req.text)
    return res['links']

"""
Send a list of urls to a jdownloader device
device: The jdownload device
urls: The list of urls to download
path: The path to download to.
"""
def jdownload(device, urls, path):
    device.linkgrabber.add_links([{'autostart': True, 'links': '\n'.join(urls), 'destinationFolder': path + "", "overwritePackagizerRules": True}])
    app.logger.info("Sent movie to jdownloader server with path: %s" % path)

"""
Setup jdownloader using the given username and password
returns: The device to send downloads to.
"""
def setup_jdownload():
    jd = myjdapi.Myjdapi()
    jd.set_app_key("JDRD")
    jd.connect(JDOWNLOADER_USER, JDOWNLOADER_PASS)
    jd.update_devices()
    device = jd.get_device(JDOWNLOADER_DEVICE)
    return device

"""
Get all the download links for a given real debrid url.
url: The real debred url to open.
"""
def get_dl_links(urls):
    download_urls = []
    for url in urls:
        req = requests.post(REAL_DB_SERVER + "unrestrict/link", data={'link': url}, headers=header)
        res = json.loads(req.text)
        if(req.status_code == 401 or req.status_code == 403):
            raise Exception("Failed to get torrent info. Status code: " + str(req.status_code))
        try:
            download_urls.append(res['download'])
        except:
            pass
    return download_urls

"""
Send a magnet url to realdebrid to being downloading.
magnet: The url.
returns: 
"""
def send_to_rd(magnet):
    data = {'magnet': magnet}
    req = requests.post(REAL_DB_SERVER + "torrents/addMagnet", data=data, headers=header)
    if req.status_code != 201:
        return (False, "Error in sending magnet link to RD. Code: %d, Text: %s" % (req.status_code, req.text))
    else:
        res = json.loads(req.text)
        ident = res['id']
        req = requests.post(REAL_DB_SERVER + "torrents/selectFiles/%s" % ident, data={'files': "all"}, headers=header)
        if req.status_code != 204 and req.status_code != 202:
            return (False, "Error in sending magnet link to RD. Code: %d, Text: %s" % (req.status_code, req.text))
        else:
            return (True, ident)

"""
Download the ID provided when it is done by RD
id: The realdebrid internal id.
"""
def download_id(id):
    urls = get_rd_download_urls(id)
    download_urls = []
    for url in urls:
        req = requests.post(REAL_DB_SERVER + "unrestrict/link", data={'link': url}, headers=header)
        res = json.loads(req.text)
        if(req.status_code == 401 or req.status_code == 403):
            app.logger.error("Failed to get torrent info. Status code: " + str(req.status_code))
            continue
        download_urls.append(res['download'])
    jdownload(device, download_urls, watched_content[id])

"""
Poll RD every rate_delay seconds in order to check for updates on torrent statuses
"""
def rd_listener():
    try:
        req = requests.get(REAL_DB_SERVER + "torrents", headers=header)
    except:
        app.logger.error("Request Issue. Maybe polling too quickly?")
        return

    # Check if we failed to connect.
    if(req.status_code == 401 or req.status_code == 403):
        app.logger.error("Failed to connect to real debrid. Error code: " + str(req.status_code))
        return

    res = json.loads(req.text)

    # For each of the different torrent files we obtained
    for file in res:
        if file['id'] in watched_content.keys():
            # If its downloaded and ready, process and remove for next cycle. Otherwise if error log and remove.
            if file['status'] == 'downloaded':
                download_id(file['id'])
                del watched_content[file['id']]
            elif file['status'] == 'magnet_error':
                app.logger.error("Magnet error on torrent with id: %s, path: %s" % (file['id'], watched_content[file['id']]))
                del watched_content[file['id']]
                continue
            elif file['status'] == 'virus':
                app.logger.error("Virus detected on torrent with id: %s, path: %s" % (file['id'], watched_content[file['id']]))
                del watched_content[file['id']]
                continue
            elif file['status'] == 'error':
                app.logger.error("Generic error on torrent with id: %s, path: %s" % (file['id'], watched_content[file['id']]))
                del watched_content[file['id']]
                continue
            elif file['status'] == 'dead':
                app.logger.error("Dead torrent with id: %s, path: %s" % (file['id'], watched_content[file['id']]))
                del watched_content[file['id']]
                continue

# Flask Routing

# Endpoint to add content to be watched
@app.route('/api/v1/content', methods=['POST'])
def addContent():
    if 'Authorization' in request.headers.keys():
        if request.headers['Authorization'] == API_KEY:
            content = request.get_json(silent=True, force=True)
            if 'magnet_url' in content:
                magnet_url = content['magnet_url']
            else:
                content = {'Error' : 'magnet_url is missing from post.'}
                return content, 400
            
            if 'path' in content:
                path = content['path']
            else:
                content = {'Error' : 'Path is missing from post.'}
                return content, 400

            # Send magnet link to be downloaded
            id = send_to_rd(magnet_url)
            if id[0] == False:
                return {'Error': id[1]}, 417

            watched_content[id[1]] = path
            return content, 200

    return {'Error' : 'Authentication Failed'}, 401

# Endpoint to get all watched content on RD
@app.route('/api/v1/content/all', methods=['GET'])
def add_content():
    if 'Authorization' in request.headers.keys():
        if request.headers['Authorization'] == API_KEY:
            return jsonify(watched_content)

    return {'Error' : 'Authentication Failed'}, 401

# Gunicorn requires this stuff ot be outside the main
device = setup_jdownload()

# Setup scheduler for checking RD
app.config.from_object(Config())
scheduler = APScheduler()
scheduler.init_app(app)
scheduler.start()

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=4248)