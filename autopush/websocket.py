import json
import time
import uuid

import cyclone.web
from autobahn.twisted.websocket import WebSocketServerProtocol
from twisted.internet import reactor
from twisted.internet.defer import (
    DeferredList,
    CancelledError
)
from twisted.internet.threads import deferToThread
from twisted.python import log


def ms_time():
    return int(time.time() * 1000)


def periodic_reporter(settings):
    settings.metrics.gauge("update.client.connections",
                           len(settings.clients))


class SimplePushServerProtocol(WebSocketServerProtocol):
    # Testing purposes
    parent_class = WebSocketServerProtocol

    def log_err(self, failure):
        log.err(failure)

    def onConnect(self, request):
        if request:
            self._user_agent = request.headers.get("user-agent")
        else:
            self._user_agent = None
        self.base_tags = []
        if self._user_agent:
            self.base_tags.append("user-agent:%s" % self._user_agent)
        self._should_stop = False
        self.metrics = self.ap_settings.metrics
        self.metrics.increment("client.socket.connect", tags=self.base_tags)
        self.uaid = None
        self.last_ping = 0
        self.accept_notification = True
        self.check_storage = False
        self.connected_at = ms_time()

        self._check_notifications = False

        # Hanger for common actions we defer
        self._notification_fetch = None
        self._register = None

        # Reflects updates sent that haven't been ack'd
        self.updates_sent = {}

        # Track notifications we don't need to delete separately
        self.direct_updates = {}
        self.channels = set()
        self.pinger = None

    #############################################################
    #                    Connection Methods
    #############################################################
    def processHandshake(self):
        """Disable host port checking on nonstandard ports since some
        clients are buggy and don't provide it"""
        port = self.ap_settings.port
        hide = port != 80 and port != 443
        if not hide:
            return self.parent_class.processHandshake(self)

        old_port = self.factory.externalPort
        try:
            self.factory.externalPort = None
            return self.parent_class.processHandshake(self)
        finally:
            self.factory.externalPort = old_port

    def onMessage(self, payload, isBinary):
        if isBinary:
            self.sendClose()
            return

        try:
            data = json.loads(payload.decode('utf8'))
        except:
            self.sendClose()
            return

        # Without a UAID, hello must be next
        if not self.uaid:
            return self.process_hello(data)

        # Ping's get a ping reply
        if data == {}:
            return self.process_ping()

        # Message needs a type
        if "messageType" not in data:
            self.sendClose()
            return

        cmd = data["messageType"]
        if cmd == "hello":
            return self.process_hello(data)
        elif cmd == "register":
            return self.process_register(data)
        elif cmd == "unregister":
            return self.process_unregister(data)
        elif cmd == "ack":
            return self.process_ack(data)
        else:
            self.sendClose()

    def onClose(self, wasClean, code, reason):
        # TODO: Any notifications directly delivered but not ack'd need
        # to be punted to an endpoint router
        uaid = getattr(self, "uaid", None)
        self._should_stop = True
        if uaid:
            self.cleanUp()

    def cleanUp(self):
        self.metrics.increment("client.socket.disconnect", tags=self.base_tags)
        elapsed = (ms_time() - self.connected_at) / 1000.0
        self.metrics.timing("client.socket.lifespan", duration=elapsed,
                            tags=self.base_tags)
        if self.uaid and self.ap_settings.clients.get(self.uaid) == self:
            del self.ap_settings.clients[self.uaid]
            for defer in [self._notification_fetch, self._register]:
                if defer:
                    defer.cancel()

    def returnError(self, messageType, reason, statusCode, close=True):
        self.sendJSON({"messageType": messageType,
                       "reason": reason,
                       "status": statusCode})
        if close:
            self.sendClose()

    def sendJSON(self, body):
        self.sendMessage(json.dumps(body).encode('utf8'), False)

    #############################################################
    #                Message Processing Methods
    #############################################################
    def process_hello(self, data):
        # This must be a helo, or we kick the client
        cmd = data.get("messageType")
        if cmd != "hello":
            self.sendClose()
            return

        if self.uaid:
            self.returnError("hello", "duplicate hello", 401)
            return

        uaid = data.get("uaid")
        valid = False
        if uaid:
            try:
                valid = bool(uuid.UUID(uaid))
            except ValueError:
                pass
        if not uaid or not valid:
            uaid = str(uuid.uuid4())
        self.uaid = uaid

        connect = data.get("connect")
        if connect is not None and self.ap_settings.pinger is not None:
            self.transport.pauseProducing()
            d = deferToThread(self.ap_settings.pinger.register, uaid, connect)
            d.addCallback(self._check_router, True)
            d.addErrback(self.err_hello)
        else:
            self._check_router(False)

    def _check_router(self, paused=False):
        if paused:
            self.transport.resumeProducing()
        # User exists?
        router = self.ap_settings.router
        url = self.ap_settings.router_url

        # Attempt to register the user for this session
        self.transport.pauseProducing()
        d = deferToThread(router.register_user,
                          self.uaid, url, self.connected_at)
        d.addCallback(self.finish_hello)
        d.addErrback(self.err_hello)
        d.addErrback(self.log_err)
        self._register = d
        return d

    def err_hello(self, failure):
        self.transport.resumeProducing()
        self.returnError("hello", "error", 503)

    def finish_hello(self, result):
        self.transport.resumeProducing()
        self._register = None
        if not result:
            # Registration failed
            msg = {"messageType": "hello", "reason": "already_connected",
                   "status": 500}
            self.sendMessage(json.dumps(msg).encode('utf8'), False)
            return

        msg = {"messageType": "hello", "uaid": self.uaid, "status": 200}
        self.ap_settings.clients[self.uaid] = self
        self.sendJSON(msg)
        self.metrics.increment("updates.client.hello", tags=self.base_tags)
        self.process_notifications()

    def process_notifications(self, tries=0):
        # Bail immediately if we are closed.
        if self._should_stop:
            return

        # Are we already running?
        if self._notification_fetch:
            # Cancel the prior, last one wins
            self._notification_fetch.cancel()

        self._check_notifications = False

        # Prevent repeat calls
        d = deferToThread(self.ap_settings.storage.fetch_notifications,
                          self.uaid)
        d.addErrback(self.cancel_notifications)
        d.addErrback(self.error_notifications, tries, d)
        d.addCallback(self.finish_notifications)
        self._notification_fetch = d

    def cancel_notifications(self, fail):
        # Don't do anything else, we got cancelled
        fail.trap(CancelledError)

    def error_notifications(self, fail, tries, d):
        # Ignore errors, but we must re-run this if it failed
        self.log_err(fail)

        # If we're running, and its not us, or we already were cleared, then
        # don't reschedule
        if self._notification_fetch is not d:
            return

        self._notification_fetch = None
        self._check_notifications = False
        if tries < 3:
            # Exponential back-off on retries
            self._notification_success = False
            reactor.callLater(tries*2+1, self.process_notifications, tries+1)

    def finish_notifications(self, notifs):
        self._notification_fetch = None

        updates = []
        notifs = notifs or []
        # Track outgoing, screen out things we've seen that weren't
        # ack'd yet
        for s in notifs:
            chid = s['chid']
            version = int(s['version'])
            if self.updates_sent.get(chid, 0) >= version:
                continue
            self.updates_sent[chid] = version
            updates.append({"channelID": chid, "version": version})
        if updates:
            # If we need to send notifications, we now expect a response
            # before any more notification processing
            self.accept_notification = False
            msg = {"messageType": "notification", "updates": updates}
            self.sendJSON(msg)

        # Were we told to check notifications again?
        if self._check_notifications:
            self._check_notifications = False
            reactor.callLater(1, self.process_notifications)

    def process_ping(self):
        now = time.time()
        if now - self.last_ping < self.ap_settings.min_ping_interval:
            self.metrics.increment("updates.client.too_many_pings",
                                   tags=self.base_tags)
            return self.sendClose()
        self.last_ping = now
        self.metrics.increment("updates.client.ping")
        return self.sendMessage("{}", False)

    def process_register(self, data):
        if "channelID" not in data:
            return self.bad_message("register")
        chid = data["channelID"]
        try:
            uuid.UUID(chid)
        except ValueError:
            return self.bad_message("register")
        self.transport.pauseProducing()

        d = deferToThread(
            self.ap_settings.makeEndpoint,
            self.uaid,
            chid)
        d.addCallbacks(self.finish_register, self.error_register,
                       callbackArgs=(chid,))

    def error_register(self, fail):
        self.transport.resumeProducing()
        msg = {"messageType": "register", "status": 500}
        self.sendJSON(msg)

    def finish_register(self, endpoint, chid):
        self.transport.resumeProducing()
        msg = {"messageType": "register",
               "channelID": chid,
               "pushEndpoint": endpoint,
               "status": 200
               }
        self.sendJSON(msg)
        self.metrics.increment("updates.client.register", tags=self.base_tags)

    def process_unregister(self, data):
        if "channelID" not in data:
            return self.bad_message("unregister")
        chid = data["channelID"]
        try:
            uuid.UUID(chid)
        except ValueError:
            return self.bad_message("unregister")

        self.metrics.increment("updates.client.unregister",
                               tags=self.base_tags)

        # Delete any record from storage, we don't wait for this
        d = deferToThread(self.ap_settings.storage.delete_notification,
                          self.uaid, chid)
        d.addBoth(self.force_delete, chid)
        data["status"] = 200
        self.sendJSON(data)

    def force_delete(self, result, chid):
        """Forces another delete call through until it works"""
        if result not in [True, False]:
            # This is an exception, log it
            self.log_err(result)

        d = deferToThread(self.ap_settings.storage.delete_notification,
                          self.uaid, chid)
        d.addErrback(self.force_delete, chid)

    def process_ack(self, data):
        updates = data.get("updates")
        if not updates or not isinstance(updates, list):
            return

        self.metrics.increment("updates.client.ack")
        defers = []
        for update in updates:
            chid = update.get("channelID")
            version = update.get("version")
            if not chid or not version:
                continue

            skip = False
            # We always need to delete direct updates
            if self.direct_updates.get(chid) == version:
                del self.direct_updates[chid]
                skip = True

            # If this is the same as a version we sent, delete
            # as well
            if self.updates_sent.get(chid) == version:
                del self.updates_sent[chid]
            else:
                # An ack for something we aren't tracking?
                continue

            if skip:
                continue

            # Attempt to delete this notification from storage
            storage = self.ap_settings.storage

            # TODO: Check result here, and do something if this delete fails
            # like maybe do a new storage check
            d = deferToThread(storage.delete_notification,
                              self.uaid, chid, version)
            d.addCallback(self.check_ack, self.uaid, chid, version)
            d.addErrback(self.log_err)
            defers.append(d)

        # If that was the last ack we were expecting, we're clear now
        if not self.updates_sent:
            self.accept_notification = True

        if defers:
            self.transport.pauseProducing()
            dl = DeferredList(defers)
            dl.addBoth(self.check_missed_notifications)

    def check_ack(self, result, uaid, chid, version):
        if result:
            return None

        # Retry the operation and return its new deferred
        d = deferToThread(self.ap_settings.storage.delete_notification, uaid,
                          chid, version)
        d.addCallback(self.check_ack, uaid, chid, version)
        d.addErrback(self.log_err)
        return d

    def check_missed_notifications(self, results):
        # Resume consuming ack's
        self.transport.resumeProducing()

        # If they're all ack'd, we will send notifications again
        if not self.updates_sent:
            self.accept_notification = True

            # Should we check again?
            if self._check_notifications:
                self.process_notifications()

    def bad_message(self, typ):
        msg = {"messageType": typ, "status": 401}
        self.sendJSON(msg)

    ####################################
    # Utility function for external use
    def send_notifications(self, updates):
        toSend = []
        for update in updates:
            channel_id, version = update["channelID"], update["version"]
            if (
                channel_id in self.updates_sent and
                self.updates_sent[channel_id] > version
            ):
                # Already sent a newer version for this channel, so don't
                # update our versioning
                continue

            # Otherwise we can record we sent this version
            self.direct_updates[channel_id] = version
            self.updates_sent[channel_id] = version
            toSend.append(update)

        if toSend:
            msg = {"messageType": "notification", "updates": toSend}
            self.sendJSON(msg)
            self.accept_notification = False


class RouterHandler(cyclone.web.RequestHandler):
    def put(self, uaid):
        settings = self.ap_settings
        client = settings.clients.get(uaid)
        if not client:
            self.set_status(404)
            settings.metrics.increment("updates.router.disconnected")
            return self.write("Client not connected.")

        if not client.accept_notification:
            self.set_status(503)
            settings.metrics.increment("updates.router.busy")
            return self.write("Client busy.")

        updates = json.loads(self.request.body)
        client.send_notifications(updates)
        settings.metrics.increment("updates.router.received")
        return self.write("Client accepted for delivery")


class NotificationHandler(cyclone.web.RequestHandler):
    def put(self, uaid):
        client = self.ap_settings.clients.get(uaid)
        settings = self.ap_settings
        if not client:
            self.set_status(404)
            settings.metrics.increment("updates.notification.disconnected")
            return self.write("Client not connected.")

        if not client.accept_notification:
            # Client already busy waiting for stuff, flag for check
            client._check_notifications = True
            self.set_status(202)
            settings.metrics.increment("updates.notification.flagged")
            return self.write("Flagged for Notification check")

        # Client is online and idle, start a notification check
        client.process_notifications()
        settings.metrics.increment("updates.notification.checking")
        self.write("Notification check started")
