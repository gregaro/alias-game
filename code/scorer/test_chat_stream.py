"""Unit tests for the gRPC chat transport; no live broadcast required."""

import unittest
from unittest.mock import patch

import grpc

import chat_stream
import stream_list_pb2


class _Channel:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _Scorer:
    def __init__(self):
        self.running = True
        self.liveChatId = "chat-id"
        self.page_token = "initial"
        self.items = []

    def handle_message(self, item):
        self.items.append(item)
        self.running = False


class ChatStreamTests(unittest.TestCase):
    @patch.object(chat_stream.grpc, "secure_channel", return_value=_Channel())
    @patch.object(chat_stream.grpc, "ssl_channel_credentials", return_value=None)
    def test_stream_converts_message_and_keeps_resume_cursor(self, *_):
        response = stream_list_pb2.LiveChatMessageListResponse(
            next_page_token="next",
            items=[stream_list_pb2.LiveChatMessage(
                id="message-id",
                snippet=stream_list_pb2.LiveChatMessageSnippet(
                    display_message="Բարև", published_at="2026-08-18T12:00:00Z"),
                author_details=stream_list_pb2.LiveChatMessageAuthorDetails(
                    channel_id="channel-id", display_name="Անի"),
            )],
        )

        class Stub:
            def __init__(self, _):
                pass

            def StreamList(self, request, metadata):
                self.request = request
                self.metadata = metadata
                return iter([response])

        scorer = _Scorer()
        with patch.object(chat_stream.stream_list_pb2_grpc,
                          "V3DataLiveChatMessageServiceStub", Stub):
            self.assertTrue(chat_stream.consume(
                scorer, lambda: "token", page_token="initial"))

        self.assertEqual(scorer.page_token, "next")
        self.assertEqual(scorer.items, [{
            "id": "message-id",
            "snippet": {
                "displayMessage": "Բարև",
                "publishedAt": "2026-08-18T12:00:00Z",
            },
            "authorDetails": {
                "channelId": "channel-id",
                "displayName": "Անի",
            },
        }])

    @patch.object(chat_stream.grpc, "secure_channel", return_value=_Channel())
    @patch.object(chat_stream.grpc, "ssl_channel_credentials", return_value=None)
    def test_permanent_error_requests_rest_fallback(self, *_):
        class Denied(grpc.RpcError):
            def code(self):
                return grpc.StatusCode.PERMISSION_DENIED

        class Stub:
            def __init__(self, _):
                pass

            def StreamList(self, request, metadata):
                def fail():
                    raise Denied()
                    yield
                return fail()

        with patch.object(chat_stream.stream_list_pb2_grpc,
                          "V3DataLiveChatMessageServiceStub", Stub):
            self.assertFalse(chat_stream.consume(
                _Scorer(), lambda: "token", page_token="initial"))

    @patch.object(chat_stream.time, "sleep", return_value=None)
    @patch.object(chat_stream.grpc, "secure_channel", return_value=_Channel())
    @patch.object(chat_stream.grpc, "ssl_channel_credentials", return_value=None)
    def test_transient_error_reconnects(self, *_):
        response = stream_list_pb2.LiveChatMessageListResponse(
            next_page_token="resumed",
            items=[stream_list_pb2.LiveChatMessage(
                snippet=stream_list_pb2.LiveChatMessageSnippet(
                    display_message="ok"),
                author_details=stream_list_pb2.LiveChatMessageAuthorDetails(
                    channel_id="channel-id"),
            )],
        )

        class Unavailable(grpc.RpcError):
            def code(self):
                return grpc.StatusCode.UNAVAILABLE

        class Stub:
            calls = 0

            def __init__(self, _):
                pass

            def StreamList(self, request, metadata):
                Stub.calls += 1
                if Stub.calls == 1:
                    def fail():
                        raise Unavailable()
                        yield
                    return fail()
                return iter([response])

        scorer = _Scorer()
        with patch.object(chat_stream.stream_list_pb2_grpc,
                          "V3DataLiveChatMessageServiceStub", Stub):
            self.assertTrue(chat_stream.consume(
                scorer, lambda: "token", page_token="initial"))

        self.assertEqual(Stub.calls, 2)
        self.assertEqual(scorer.page_token, "resumed")


if __name__ == "__main__":
    unittest.main()
