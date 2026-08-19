"""Low-quota YouTube live-chat transport using streamList over gRPC.

The generated ``stream_list_pb2*.py`` modules come from the small compatible
schema beside this file. Regenerate them after editing the schema with:

    python -m grpc_tools.protoc -I code/scorer --python_out=code/scorer \
        --grpc_python_out=code/scorer code/scorer/stream_list.proto
"""

import time

import grpc

import stream_list_pb2
import stream_list_pb2_grpc


ENDPOINT = "dns:///youtube.googleapis.com:443"
FALLBACK_AFTER_ERRORS = 3

# Permanent errors mean this project/account cannot use streamList as invoked;
# retrying the same RPC would only delay the proven REST fallback.
_PERMANENT = {
    grpc.StatusCode.INVALID_ARGUMENT,
    grpc.StatusCode.PERMISSION_DENIED,
    grpc.StatusCode.UNIMPLEMENTED,
}


def as_rest_item(message) -> dict:
    """Adapt the protobuf fields to the shape Scorer.handle_message expects."""
    return {
        "id": message.id,
        "snippet": {
            "displayMessage": message.snippet.display_message,
            "publishedAt": message.snippet.published_at,
        },
        "authorDetails": {
            "channelId": message.author_details.channel_id,
            "displayName": message.author_details.display_name,
        },
    }


def consume(scorer, token_provider, page_token=None) -> bool:
    """Push messages into ``scorer`` until it stops.

    Returns False only when repeated/permanent gRPC failures make the caller's
    REST fallback the safer choice. A reconnect resumes at the last token so a
    transient network break neither loses nor deliberately replays messages.
    """
    failures = 0
    backoff = 1

    with grpc.secure_channel(ENDPOINT, grpc.ssl_channel_credentials()) as channel:
        stub = stream_list_pb2_grpc.V3DataLiveChatMessageServiceStub(channel)

        while scorer.running:
            request = stream_list_pb2.LiveChatMessageListRequest(
                live_chat_id=scorer.liveChatId,
                part=["snippet", "authorDetails"],
                page_token=page_token or "",
            )
            metadata = (("authorization", "Bearer " + token_provider()),)

            try:
                received_response = False
                for response in stub.StreamList(request, metadata=metadata):
                    if not scorer.running:
                        return True
                    received_response = True
                    failures = 0
                    backoff = 1
                    if response.next_page_token:
                        page_token = response.next_page_token
                        # If streaming eventually falls back to REST, resume at
                        # this exact cursor rather than replaying the session.
                        scorer.page_token = page_token
                    for message in response.items:
                        scorer.handle_message(as_rest_item(message))
                    if response.offline_at:
                        return True

                # A clean close while the broadcast is still live is unusual;
                # reconnect from the last page token rather than switching
                # transports immediately.
                if received_response:
                    time.sleep(0.5)
                else:
                    failures += 1
            except grpc.RpcError as exc:
                failures += 1
                code = exc.code()
                print(f"\n[stream] connection error ({code.name}); "
                      f"attempt {failures}/{FALLBACK_AFTER_ERRORS}")
                if code in _PERMANENT:
                    return False

            if failures >= FALLBACK_AFTER_ERRORS:
                return False
            time.sleep(backoff)
            backoff = min(backoff * 2, 15)

    return True
