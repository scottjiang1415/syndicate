#!/usr/bin/env python

"""
   Copyright 2013 The Trustees of Princeton University

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.
"""

import os
import sys

import bottle
from bottle import Bottle, request, response, abort, static_file

from syndicatemail.common import http as http_common

import syndicatemail.common.contact as contact
import syndicatemail.common.message as message
import syndicatemail.common.storage as storage
import syndicatemail.common.conf as conf
import syndicatemail.common.main as main_common
import syndicatemail.common.keys as keys
import syndicatemail.common.jsonrpc as jsonrpc
import syndicatemail.common.session as session

from api import API

#-------------------------
CONFIG_DIR = "/tmp/syndicatemail"               # overwritten at runtime
CONFIG_FILENAME = os.path.join( CONFIG_DIR, "syndicatemail.conf" )

CONFIG_OPTIONS = {
   "MS":                ("-M", 1, "URL to your Syndicate MS"),
   "config":            ("-c", 1, "Path to your config file (default is %s)" % CONFIG_FILENAME),
   "debug":             ("-d", 0, "Verbose debugging output"),
   "volume":            ("-V", 1, "Name of the Syndicate Volume to use"),
   "local_storage_root":      ("-r", 1, "Local storage root"),
   "volume_storage_root":     ("-R", 1, "Volume storage root"),
   "user_id":           ("-u", 1, "Syndicate user ID for mounting the Volume"),
   "user_password":     ("-p", 1, "Syndicate Volume password"),
   "mail_server":       ("-s", 1, "Mail server hostname"),
   "app":               ("-a", 1, "Path to application static files"),
   "test":              ("-t", 0, "Testing")
}

DEFAULT_OPTIONS = {
   "local_storage_root": os.path.expanduser( "~/.syndicatemail" ),
   "volume_storage_root": "/apps/syndicatemail/storage"
}
   
#-------------------------
app = Bottle()
json_server = None     # initialized at runtime
config = None          # initialized at runtime

#-------------------------
@app.post('/api')
def api_dispatch():
   global json_server
   
   json_dict = request.json
   if json_dict is None:
      app.abort(400, "JSON API Request Handler")
   
   return json_server.handle( None, data=json_dict )

#-------------------------
@app.get('/app/<filename:path>')
def serve_app_data():
   return static_file( filename, config['app'] )


#-------------------------
if __name__ == "__main__":
   config = main_common.load_options( sys.argv, "SyndicateMail Endpoint", CONFIG_OPTIONS, CONFIG_FILENAME )
   
   missing = False
   for required_option in ['local_storage_root', 'volume_storage_root', 'app', 'volume', 'user_id', 'user_password', 'mail_server', 'MS']:
      if required_option not in config:
         print >> sys.stderr, "Missing option: %s" % required_option
         missing = True
   
   if missing:
      conf.usage( sys.argv[0], "SyndicateMail Endpoint", CONFIG_OPTIONS )
   
   if config['test']:
      session.do_test_volume( config['volume_storage_root'] )
      
   rc = main_common.setup_storage( config )
   if not rc:
      raise Exception("Failed to set up storage")
   
   if config['test']:
      session.do_test_login( config, "testuser.mail.syndicate.com@example.edu", "poop" )
   
   API.setup( config )
   json_server = jsonrpc.Server( API )
   
   # start serving
   app.run( host="localhost", port=33333, debug=config['debug'] )
      
   