# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import inspect
import logging
import os

from wampy.constants import DEFAULT_ROLES, DEFAULT_REALM
from wampy.errors import (
    WampProtocolError, WampyError, WelcomeAbortedError)
from wampy.session import Session
from wampy.messages import Abort, Challenge, Welcome
from wampy.message_handler import MessageHandler
from wampy.peers.routers import Router
from wampy.roles.caller import CallProxy, RpcProxy
from wampy.roles.publisher import PublishProxy
from wampy.transports import WebSocket

logger = logging.getLogger("wampy.clients")


class Client(object):
    """ A WAMP Client for use in Python applications, scripts and shells.
    """

    def __init__(
            self,
            host=None, port=None,
            realm=DEFAULT_REALM, roles=DEFAULT_ROLES,
            message_handler=None, transport=None, name=None,
            router=None
    ):
        """ A WAMP Client "Peer".

        WAMP is designed for application code to run within Clients,
        i.e. Peers.

        Peers have the roles Callee, Caller, Publisher, and Subscriber.

        Subclass this base class to implemente the Roles for your application.

        :Parameters:
            realm : str
                The routing namespace to construct the ``Session`` over.
                Defaults to ``realm1``.
            roles : dictionary
                Description of the Roles implemented by the ``Client``.
                Defaults to ``wampy.constants.DEFAULT_ROLES``.
            host: str
                The endpoint of the Router, e.g. "ws://example.com" or
                "wss://example.com"
            port: int
                The port on the endpoint that provides the WAMP connection.
            message_handler : instance
                An instance of ``wampy.message_handler.MessageHandler``, or
                a subclass of. This controls the conversation between the
                two Peers.
            transport : instance
                An instance of ``wampy.transports``.
                Defaults to ``wampy.transports.WebSocket``
            name : string
                Optional name for your ``Client``. Useful for when testing
                your app or for logging.

        """
        # the ``realm`` is the administrive domain to route messages over.
        self.realm = realm
        # the ``roles`` define what Roles (features) the Client can act,
        # but also configure behaviour such as auth
        self.roles = roles

        # a Session is a transient conversation between two Peers - a Client
        # and a Router. Here we model the Peer we are going to connect to.
        self.router = router or Router(host=host, port=port)
        # wampy uses a decoupled "messge handler" to process incoming messages.
        # wampy also provides a very adequate default.
        self.message_handler = message_handler or MessageHandler()
        # this conversation is over a transport. WAMP messages are transmitted
        # as WebSocket messages by default.
        self.transport = transport or WebSocket()
        # the transport is responsible for the connection.
        self.transport.register_router(self.router)

        # generally ``name`` is used for debuggubg and logging only
        self.name = name or self.__class__.__name__

        self._session = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exception_type, exception_value, traceback):
        self.stop()

    @property
    def session(self):
        return self._session

    @property
    def subscription_map(self):
        return self.session.subscription_map

    @property
    def registration_map(self):
        return self.session.registration_map

    @property
    def request_ids(self):
        return self.session.request_ids

    def start(self):
        # establish the underlying connection. this will raise on error.
        connection = self.transport.connect()

        # create a Session repr between ourselves and the Router.
        # pass in the live connection, over a transport that the Session
        # doesn't need to know about - it only knows how to receieve
        # over this.
        # pass in out ``MessageHandler`` which will process messages
        # before they are passed back to the client.
        self._session = Session(
            client=self,
            router=self.router,
            connection=connection,
            message_handler=self.message_handler,
        )

        # establish the session
        response = self.session.begin()

        # TODO: THIS SHOULD ALL BE IN THE MESSAGE HANDLER OR SESSION
        if response.WAMP_CODE == Abort.WAMP_CODE:
            raise WelcomeAbortedError(response.message)

        if response.WAMP_CODE == Challenge.WAMP_CODE:
            if 'WAMPYSECRET' not in os.environ:
                raise WampyError(
                    "Wampy requires a client's secret to be "
                    "in the environment as ``WAMPYSECRET``"
                )

            raise WampyError("Failed to handle CHALLENGE")

        if response.WAMP_CODE == Welcome.WAMP_CODE:
            logger.info("client %s has connected", self.name)

    def stop(self):
        if self.session and self.session.id:
            self.session.end()

        self.transport.disconnect()
        self._session = None

    def send_message(self, message):
        self.session.send_message(message)

    def recv_message(self):
        return self.session.recv_message()

    def make_rpc(self, message):
        logger.debug("%s sending message: %s", self.name, message)

        self.session.send_message(message)

        try:
            response = self.session.recv_message()
        except WampProtocolError as wamp_err:
            logger.error(wamp_err)
            raise
        except Exception as exc:
            logger.warning("rpc failed!!")
            logger.exception(str(exc))
            raise

        return response

    @property
    def call(self):
        return CallProxy(client=self)

    @property
    def rpc(self):
        return RpcProxy(client=self)

    @property
    def publish(self):
        return PublishProxy(client=self)

    def register_roles(self):
        # over-ride this if you want to customise how your client regisers
        # its Roles
        logger.info("registering roles for: %s", self.name)

        maybe_roles = []
        bases = [b for b in inspect.getmro(self.__class__) if b is not object]

        for base in bases:
            maybe_roles.extend(
                v for v in base.__dict__.values() if
                inspect.isclass(base) and callable(v)
            )

        for maybe_role in maybe_roles:

            if hasattr(maybe_role, 'callee'):
                procedure_name = maybe_role.__name__
                invocation_policy = maybe_role.invocation_policy
                self.session._register_procedure(
                    procedure_name, invocation_policy)

                logger.info(
                    '%s registered callee "%s"', self.name, procedure_name,
                )

            if hasattr(maybe_role, 'subscriber'):
                topic = maybe_role.topic
                handler_name = maybe_role.handler.__name__
                handler = getattr(self, handler_name)
                self.session._subscribe_to_topic(handler, topic)
                logger.info(
                    '%s subscribed to topic "%s"', self.name, topic,
                )
