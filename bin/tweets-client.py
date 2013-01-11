#!/usr/bin/env python
# encoding: utf-8
"""
tweets-client.py

Created by Breyten Ernsting on 2010-05-30.
Copyright (c) 2010 Back-End-Front Web Development. All rights reserved.
"""

import os
import sys
import argparse
import signal
import ConfigParser

import socket
# disable buffering
socket._fileobject.default_bufsize = 0

import httplib
httplib.HTTPConnection.debuglevel = 1

import anyjson
import logbook

# this is for consuming the streaming API
import tweepy
import tweetsclient
import politwoops


_script_ = (os.path.basename(__file__)
            if __name__ == "__main__"
            else __name__)
log = logbook.Logger(_script_)

class DataRecord(object):
    def __init__(self, *args, **kwargs):
        object.__setattr__(self, '_dict', {})
        self._dict.update(((arg, None) for arg in args))
        self._dict.update(kwargs)

    def __getattr__(self, attr):
        if attr not in self._dict:
            raise AttributeError("{cls!r} has no attribute {attr!r}".format(cls=self.__class__.__name__,
                                                                        attr=attr))
        return self._dict[attr]

    def __setattr__(self, attr, value):
        raise AttributeError("All attributes of DataRecord objects are read-only")
            

class Usage(Exception):
    def __init__(self, msg):
        self.msg = msg

class TweetListener(tweepy.streaming.StreamListener):
    def __init__(self, queue, *args, **kwargs):
        super(TweetListener, self).__init__(*args, **kwargs)
        self.queue = queue

    def on_data(self, data):
        try:
            self.queue.add(data)
        except Exception as e:
            log.error(u"TweetListener.on_data() caught exception: {0}".format(unicode(e)))
            raise

    def on_error(self, status):
        log.error(u"TweetListener.on_data() caught exception: {0}".format(unicode(status)))

class TweetStreamClient(object):
    def __init__(self):
        self.config = tweetsclient.Config().get()
        consumer_key = self.config.get('tweets-client', 'consumer_key')
        consumer_secret = self.config.get('tweets-client', 'consumer_secret')
        access_token = self.config.get('tweets-client', 'access_token')
        access_token_secret = self.config.get('tweets-client', 'access_token_secret')
        log.debug("Consumer credentials: {key}, {secret}",
                  key=consumer_key,
                  secret=consumer_secret)
        log.debug("Access credentials: {token}, {secret}",
                  token=access_token,
                  secret=access_token_secret)
        self.twitter_auth = tweepy.OAuthHandler(consumer_key, consumer_secret)
        self.twitter_auth.set_access_token(access_token, access_token_secret)
        try:
            username = self.twitter_auth.get_username()
            log.notice("Authenticated as {user}".format(user=username))
        except tweepy.error.TweepError as e:
            log.error(unicode(e))

    def get_config_default(self, section, key, default = None):
        try:
            return self.config.get(section, key)
        except ConfigParser.NoOptionError:
            return default
        
    def load_plugin(self, plugin_module, plugin_class):
        pluginModule = __import__(plugin_module)
        components = plugin_module.split('.')
        for comp in components[1:]:
            pluginModule = getattr(pluginModule, comp)
        pluginClass = getattr(pluginModule, plugin_class)
        return pluginClass

    def init_beanstalk(self):
        tweets_tube = self.config.get('beanstalk', 'tweets_tube')

        log.info("Initiating beanstalk connection. Queueing tweets to {use}...", use=tweets_tube)

        self.beanstalk = politwoops.utils.beanstalk(host=self.config.get('beanstalk', 'host'),
                                                    port=int(self.config.get('beanstalk', 'port')),
                                                    watch=None,
                                                    use=tweets_tube)
    
    def stream_forever(self):
        queue_module = self.get_config_default('tweets-client', 'track-module', 'tweetsclient.config_track')
        queue_class = self.get_config_default('tweets-client', 'track-class', 'ConfigTrackPlugin')
        log.debug("Loading track plugin: {module} - {klass}",
                  module=queue_module, klass=queue_class)

        pluginClass = self.load_plugin(queue_module, queue_class)
        self.track = pluginClass()
        #self.track = tweetsclient.MySQLTrackPlugin({'verbose': self.verbose})
        # self.track = tweetsclient.ConfigTrackPlugin({'verbose': self.verbose})
        stream_type = self.track.get_type()
        log.debug("Initializing a {0} stream of tweets.", stream_type)
        track_items = self.track.get_items()
        log.debug(str(track_items))

        stream = None
        if stream_type == 'users':
            tweet_listener = TweetListener()
            stream = tweepy.Stream(self.twitter_auth, tweet_listener)
            stream.filter(follow=track_items)
        elif stream_type == 'words':
            raise Exception('The words stream type is no longer supported.')
        else:
            raise Exception('Unrecognized stream type: {0}'.format(stream_type))
    
    def run(self):
        self.init_beanstalk()
        with politwoops.utils.Heart() as heart:
            politwoops.utils.start_heartbeat_thread(heart)
            politwoops.utils.start_watchdog_thread(heart)
            self.stream_forever()

        self.beanstalk.disconnect()
        return 0
 
    def handle_tweet(self, stream, tweet):
        # reset the restart counter once a tweet has come in
        self.restartCounter = 0
        # add the tweet to the queue
        log.info(u"Queued tweet {0}", tweet)
        if tweet.has_key('user'):
            log.notice(u"Queued tweet for user {0}/{1}".format(tweet.get('user').get('screen_name', ''), tweet.get('user').get('id_str')))
        else:
            log.notice(u"Queued tweet: {0}".format(tweet))

        self.beanstalk.put(anyjson.serialize(tweet))

def main(args):
    signal.signal(signal.SIGHUP, politwoops.utils.restart_process)

    log_handler = politwoops.utils.configure_log_handler(_script_, args.loglevel, args.output)
    with logbook.NullHandler():
        with log_handler.applicationbound():
            log.debug("Starting tweets-client.py")
            try:
                app = TweetStreamClient()
                if args.authtest:
                    return
                if args.restart:
                    return politwoops.utils.run_with_restart(app.run)
                else:
                    return app.run()
            except KeyboardInterrupt:
                log.notice("Killed by CTRL-C")

if __name__ == "__main__":
    args_parser = argparse.ArgumentParser(description=__doc__)
    args_parser.add_argument('--loglevel', metavar='LEVEL', type=str,
                             help='Logging level (default: notice)',
                             default='notice',
                             choices=('debug', 'info', 'notice', 'warning',
                                      'error', 'critical'))
    args_parser.add_argument('--output', metavar='DEST', type=str,
                             default='-',
                             help='Destination for log output (-, syslog, or filename)')
    args_parser.add_argument('--restart', default=False, action='store_true',
                             help='Restart when an error cannot be handled.')
    args_parser.add_argument('--authtest', default=False, action='store_true',
                             help='Authenticate against Twitter and exit.')
    args = args_parser.parse_args()
    sys.exit(main(args))
