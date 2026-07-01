import asyncio
import logging

import config
from starlette.requests import Request
from starlette.responses import RedirectResponse
from starlette.routing import Route, WebSocketRoute, Mount
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket

from api import (
    auth_routes,
    handy_controller,
    image_routes,
    library_routes,
    live_tv_admin_routes,
    live_tv_routes,
    metadata_routes,
    stream_routes,
    ui_routes,
    userdata_routes,
)

logger = logging.getLogger(__name__)


async def _dummy_websocket(websocket: WebSocket):
    import json as _json
    import uuid as _uuid
    await websocket.accept()
    logger.debug("WebSocket connection opened")

    async def _keepalive():
        # Send ForceKeepAlive every 25 s.  MessageId is required by the Jellyfin
        # SDK's Kotlin serializer — omitting it crashes the client app.
        while True:
            try:
                await websocket.send_text(_json.dumps({
                    "MessageType": "ForceKeepAlive",
                    "MessageId": str(_uuid.uuid4()),
                    "Data": 30,
                }))
            except Exception:
                break
            await asyncio.sleep(25)

    ka_task = asyncio.create_task(_keepalive())
    try:
        while True:
            msg = await websocket.receive_text()
            logger.debug(f"WebSocket recv: {msg[:120]}")
    except Exception:
        pass
    finally:
        ka_task.cancel()


async def _root_router(request: Request):
    ui_port = getattr(config, "UI_PORT", 8097)
    if request.url.port == ui_port:
        return await ui_routes.serve_index(request)
    return RedirectResponse(url="/web/index.html", status_code=302)


routes = [
    Route("/", _root_router, methods=["GET"]),
    Route("/favicon.ico", lambda r: RedirectResponse(url="/web/favicon.ico", status_code=302), methods=["GET"]),

    Route("/api/config", ui_routes.api_get_config, methods=["GET"]),
    Route("/api/config", ui_routes.api_post_config, methods=["POST"]),
    Route("/api/logs", ui_routes.api_get_logs, methods=["GET"]),
    Route("/api/logs/clear", ui_routes.api_clear_logs, methods=["POST"]),
    Route("/api/status", ui_routes.api_get_status, methods=["GET"]),
    Route("/api/restart", ui_routes.api_restart, methods=["POST"]),
    Route("/api/streams", ui_routes.api_get_streams, methods=["GET"]),
    Route("/api/stats", ui_routes.api_get_stats, methods=["GET"]),
    Route("/api/stats/reset", ui_routes.api_reset_stats, methods=["POST"]),
    Route("/api/auth/check", ui_routes.api_auth_check, methods=["GET"]),
    Route("/api/auth/login", ui_routes.api_login, methods=["POST"]),
    Route("/api/auth/logout", ui_routes.api_logout, methods=["POST"]),
    Route("/api/login", ui_routes.api_login, methods=["POST"]),
    Route("/api/logout", ui_routes.api_logout, methods=["POST"]),
    Route("/api/auth/dynamic_ips/{ip}", ui_routes.api_prune_dynamic_ip, methods=["DELETE"]),
    Route("/api/cache/increment", ui_routes.api_increment_cache_version, methods=["POST"]),
    Route("/api/cache/clear", ui_routes.api_clear_cache, methods=["POST"]),
    Route("/api/stats/top_played", ui_routes.api_clear_top_played, methods=["DELETE"]),
    Route("/api/stats/top_played/{item_id}", ui_routes.api_remove_top_played_item, methods=["DELETE"]),
    Route('/api/quickconnect/authorize', auth_routes.endpoint_quickconnect_authorize, methods=['POST']),
    Route("/api/sysinfo", ui_routes.api_get_sysinfo, methods=["GET"]),
    Route("/api/livetv/rebuild-schedule", live_tv_admin_routes.endpoint_rebuild_schedule, methods=["POST"]),
    Route("/api/livetv/guide", live_tv_admin_routes.endpoint_guide_data, methods=["GET"]),
    Route("/api/livetv/channel-now", live_tv_routes.endpoint_channel_now_playing, methods=["GET"]),
    Route("/api/livetv/shorts-block-preview", live_tv_routes.endpoint_shorts_block_preview, methods=["GET"]),
    # Channel config CRUD
    Route("/api/livetv/channels-config", live_tv_admin_routes.endpoint_channels_config_list, methods=["GET"]),
    Route("/api/livetv/channels-config", live_tv_admin_routes.endpoint_channels_config_create, methods=["POST"]),
    Route("/api/livetv/channels-config/reorder", live_tv_admin_routes.endpoint_channels_config_reorder, methods=["POST"]),
    Route("/api/livetv/channels-config/{tvg_id}", live_tv_admin_routes.endpoint_channels_config_update, methods=["PATCH"]),
    Route("/api/livetv/channels-config/{tvg_id}", live_tv_admin_routes.endpoint_channels_config_delete, methods=["DELETE"]),
    Route("/api/livetv/channels-config/{tvg_id}/rebuild", live_tv_admin_routes.endpoint_channel_rebuild, methods=["POST"]),
    # Handy devices (multi-device registry — see docs/handy_integration.md §9a)
    Route("/api/handy/devices", handy_controller.endpoint_devices_list, methods=["GET"]),
    Route("/api/handy/devices", handy_controller.endpoint_devices_create, methods=["POST"]),
    Route("/api/handy/devices/status", handy_controller.endpoint_devices_status, methods=["GET"]),
    Route("/api/handy/devices/{device_id}", handy_controller.endpoint_devices_update, methods=["PUT"]),
    Route("/api/handy/devices/{device_id}", handy_controller.endpoint_devices_delete, methods=["DELETE"]),
    # Stash source lists
    Route("/api/livetv/stash-tags", live_tv_admin_routes.endpoint_stash_tags_list, methods=["GET"]),
    Route("/api/livetv/stash-filters", live_tv_admin_routes.endpoint_stash_filters_list, methods=["GET"]),
    Route("/api/livetv/stash-tag-image/{tag_id}", live_tv_admin_routes.endpoint_stash_tag_image, methods=["GET"]),
    Route("/api/livetv/channel-logo/{tvg_id}/from-tag", live_tv_admin_routes.endpoint_channel_logo_set_from_tag, methods=["POST"]),
    Route("/api/livetv/channel-logo/{tvg_id}", live_tv_admin_routes.endpoint_channel_logo_get, methods=["GET"]),
    Route("/api/livetv/channel-logo/{tvg_id}", live_tv_admin_routes.endpoint_channel_logo_upload, methods=["POST"]),
    Route("/api/livetv/channel-logo/{tvg_id}", live_tv_admin_routes.endpoint_channel_logo_delete, methods=["DELETE"]),
    Route("/api/livetv/epg-scene-match", live_tv_admin_routes.endpoint_epg_scene_match, methods=["GET"]),
    Route("/api/livetv/scene/{scene_id}", live_tv_admin_routes.endpoint_scene_detail, methods=["GET"]),
    Route("/api/livetv/scene/{scene_id}/screenshot", live_tv_admin_routes.endpoint_scene_screenshot, methods=["GET"]),
    Route("/api/livetv/channel-scenes/{tvg_id}", live_tv_admin_routes.endpoint_channel_scenes, methods=["GET"]),
    Route("/api/livetv/schedule/{tvg_id}/{eid}", live_tv_admin_routes.endpoint_schedule_delete, methods=["DELETE"]),
    Route("/api/livetv/schedule/{tvg_id}/reorder", live_tv_admin_routes.endpoint_schedule_reorder, methods=["POST"]),
    Route("/api/livetv/schedule/{tvg_id}/insert", live_tv_admin_routes.endpoint_schedule_insert, methods=["POST"]),

    Route("/system/info/public", auth_routes.endpoint_system_info_public, methods=["GET"]),
    Route("/public/system/info", auth_routes.endpoint_system_info_public, methods=["GET"]),
    Route("/system/info", auth_routes.endpoint_system_info, methods=["GET"]),
    Route("/system/ping", auth_routes.endpoint_system_ping, methods=["GET", "POST"]),
    Route("/users/public", auth_routes.endpoint_public_users, methods=["GET"]),
    Route("/users/authenticatebyname", auth_routes.endpoint_authenticate_by_name, methods=["POST"]),
    Route("/users/{user_id}", auth_routes.endpoint_user, methods=["GET"]),
    Route("/users", auth_routes.endpoint_users, methods=["GET"]),
    Route('/users/authenticatewithquickconnect', auth_routes.endpoint_authenticate_by_quickconnect, methods=['POST']),
    Route('/quickconnect/enabled', auth_routes.endpoint_quickconnect_enabled, methods=['GET']),
    Route('/quickconnect/initiate', auth_routes.endpoint_quickconnect_initiate, methods=['GET', 'POST']),
    Route('/quickconnect/connect', auth_routes.endpoint_quickconnect_connect, methods=['GET']),
    Route("/branding/configuration", auth_routes.endpoint_branding_configuration, methods=["GET"]),

    Route("/userviews", library_routes.endpoint_views, methods=["GET"]),
    Route("/users/{user_id}/views", library_routes.endpoint_views, methods=["GET"]),
    Route("/library/virtualfolders", library_routes.endpoint_virtual_folders, methods=["GET"]),
    Route("/users/{user_id}/items/resume", library_routes.endpoint_resume, methods=["GET"]),
    Route("/useritems/resume", library_routes.endpoint_resume, methods=["GET"]),
    Route("/users/{user_id}/items/latest", library_routes.endpoint_latest, methods=["GET"]),
    Route("/items/latest", library_routes.endpoint_latest, methods=["GET"]),
    Route("/items/suggestions", library_routes.endpoint_empty_list, methods=["GET"]),

    Route("/sessions/capabilities", auth_routes.endpoint_system_ping, methods=["POST"]),
    Route("/movies/recommendations", library_routes.endpoint_empty_array, methods=["GET"]),
    Route("/items/filters", library_routes.endpoint_filters, methods=["GET"]),
    Route("/items/filters2", library_routes.endpoint_filters, methods=["GET"]),
    Route("/mediasegments/{item_id}", library_routes.endpoint_empty_list, methods=["GET"]),
    Route("/shows/{series_id}/episodes", library_routes.endpoint_shows_episodes, methods=["GET"]),
    Route("/shows/nextup", library_routes.endpoint_next_up, methods=["GET"]),
    Route("/genres", metadata_routes.endpoint_tags, methods=["GET"]),
    Route("/users/{user_id}/genres", metadata_routes.endpoint_tags, methods=["GET"]),
    Route("/tags", metadata_routes.endpoint_tags, methods=["GET"]),
    Route("/users/{user_id}/tags", metadata_routes.endpoint_tags, methods=["GET"]),
    Route("/years", metadata_routes.endpoint_years, methods=["GET"]),
    Route("/studios", metadata_routes.endpoint_studios, methods=["GET"]),
    Route("/persons", library_routes.endpoint_empty_list, methods=["GET"]),
    Route("/artists", library_routes.endpoint_empty_list, methods=["GET"]),

    Route("/items", library_routes.endpoint_items, methods=["GET"]),
    Route("/users/{user_id}/items", library_routes.endpoint_items, methods=["GET"]),
    Route("/search/hints", library_routes.endpoint_search_hints, methods=["GET"]),

    Route("/users/{user_id}/items/{item_id}", metadata_routes.endpoint_item_details, methods=["GET"]),
    Route("/users/{user_id}/items/{item_id}", metadata_routes.endpoint_delete_item, methods=["DELETE"]),
    Route("/items/{item_id}", metadata_routes.endpoint_item_details, methods=["GET"]),
    Route("/items/{item_id}", metadata_routes.endpoint_delete_item, methods=["DELETE"]),
    Route("/items/{item_id}/metadataeditor", metadata_routes.endpoint_metadata_editor, methods=["GET"]),
    Route("/users/{user_id}/items/{item_id}/metadataeditor", metadata_routes.endpoint_metadata_editor, methods=["GET"]),

    Route("/items/{item_id}", metadata_routes.endpoint_update_item, methods=["POST"]),
    Route("/users/{user_id}/items/{item_id}", metadata_routes.endpoint_update_item, methods=["POST"]),
    Route("/items/{item_id}/images", metadata_routes.endpoint_item_images_info, methods=["GET"]),
    Route("/users/{user_id}/items/{item_id}/images", metadata_routes.endpoint_item_images_info, methods=["GET"]),

    Route("/users/{user_id}/items/{item_id}/thememedia", library_routes.endpoint_theme_songs, methods=["GET"]),
    Route("/users/{user_id}/items/{item_id}/themesongs", library_routes.endpoint_theme_songs, methods=["GET"]),
    Route("/users/{user_id}/items/{item_id}/similar", library_routes.endpoint_empty_list, methods=["GET"]),
    Route("/users/{user_id}/items/{item_id}/specialfeatures", library_routes.endpoint_empty_array, methods=["GET"]),
    Route("/users/{user_id}/items/{item_id}/intros", library_routes.endpoint_empty_list, methods=["GET"]),
    Route("/items/{item_id}/thememedia", library_routes.endpoint_theme_songs, methods=["GET"]),
    Route("/items/{item_id}/themesongs", library_routes.endpoint_theme_songs, methods=["GET"]),
    Route("/items/{item_id}/similar", library_routes.endpoint_similar_items, methods=["GET"]),
    Route("/items/{item_id}/specialfeatures", library_routes.endpoint_empty_array, methods=["GET"]),
    Route("/items/{item_id}/intros", library_routes.endpoint_empty_list, methods=["GET"]),

    Route("/items/{item_id}/images/{image_type}", image_routes.endpoint_item_image, methods=["GET"]),
    Route("/items/{item_id}/images/{image_type}/{image_index}", image_routes.endpoint_item_image, methods=["GET"]),
    Route("/users/{user_id}/items/{item_id}/images/{image_type}", image_routes.endpoint_item_image, methods=["GET"]),
    Route("/users/{user_id}/items/{item_id}/images/{image_type}/{image_index}", image_routes.endpoint_item_image, methods=["GET"]),
    Route("/users/{item_id}/images/{image_type}", image_routes.endpoint_item_image, methods=["GET"]),
    Route("/videos/{item_id}/trickplay/{width}/{file_name}", image_routes.endpoint_trickplay_image, methods=["GET"]),

    Route("/users/{user_id}/items/{item_id}/playbackinfo", stream_routes.endpoint_playback_info, methods=["POST", "GET"]),
    Route("/items/{item_id}/playbackinfo", stream_routes.endpoint_playback_info, methods=["POST", "GET"]),

    Route("/videos/{item_id}/subtitles/{stream_index}/stream.{format}", stream_routes.endpoint_subtitle, methods=["GET"]),
    Route("/videos/{item_id}/hls/{segment}", stream_routes.endpoint_hls_segment, methods=["GET"]),
    Route("/videos/{item_id}/master.m3u8", stream_routes.endpoint_stream, methods=["GET", "HEAD"]),
    Route("/videos/{item_id}/main.m3u8", stream_routes.endpoint_stream, methods=["GET", "HEAD"]),
    Route("/videos/{item_id}/stream.mp4", stream_routes.endpoint_stream, methods=["GET", "HEAD"]),
    Route("/videos/{item_id}/stream", stream_routes.endpoint_stream, methods=["GET", "HEAD"]),

    Route("/sessions/playing", userdata_routes.endpoint_sessions_playing, methods=["POST"]),
    Route("/sessions/playing/progress", userdata_routes.endpoint_sessions_playing, methods=["POST"]),
    Route("/sessions/playing/stopped", userdata_routes.endpoint_sessions_stopped, methods=["POST"]),

    Route("/users/{user_id}/playeditems/{item_id}", userdata_routes.endpoint_mark_played, methods=["POST"]),
    Route("/users/{user_id}/playeditems/{item_id}", userdata_routes.endpoint_mark_unplayed, methods=["DELETE"]),
    Route("/userplayeditems/{item_id}", userdata_routes.endpoint_mark_played, methods=["POST"]),
    Route("/userplayeditems/{item_id}", userdata_routes.endpoint_mark_unplayed, methods=["DELETE"]),
    Route("/useritems/{item_id}/userdata", userdata_routes.endpoint_update_userdata, methods=["POST"]),
    Route("/users/{user_id}/items/{item_id}/userdata", userdata_routes.endpoint_update_userdata, methods=["POST"]),

    Route("/users/{user_id}/favoriteitems/{item_id}", userdata_routes.endpoint_mark_favorite, methods=["POST"]),
    Route("/users/{user_id}/favoriteitems/{item_id}", userdata_routes.endpoint_unmark_favorite, methods=["DELETE"]),
    Route("/userfavoriteitems/{item_id}", userdata_routes.endpoint_mark_favorite, methods=["POST"]),
    Route("/userfavoriteitems/{item_id}", userdata_routes.endpoint_unmark_favorite, methods=["DELETE"]),

    Route("/displaypreferences/{display_id}", library_routes.endpoint_display_preferences, methods=["GET", "POST"]),
    Route("/users/{user_id}/displaypreferences/{display_id}", library_routes.endpoint_display_preferences, methods=["GET", "POST"]),
    Route("/users/{user_id}/policy", auth_routes.endpoint_user, methods=["GET"]),
    Route("/users/{user_id}/configuration", auth_routes.endpoint_user, methods=["GET"]),

    Route("/items/{item_id}/download", stream_routes.endpoint_stream, methods=["GET"]),
    Route("/users/{user_id}/items/{item_id}/download", stream_routes.endpoint_stream, methods=["GET"]),

    Route("/livetv/info", live_tv_routes.endpoint_live_tv_info, methods=["GET"]),
    Route("/livetv/guideinfo", live_tv_routes.endpoint_guide_info, methods=["GET"]),
    Route("/livetv/channels", live_tv_routes.endpoint_channels, methods=["GET"]),
    Route("/livetv/channels/{channel_id}", live_tv_routes.endpoint_channel_single, methods=["GET"]),
    Route("/livetv/programs/recommended", live_tv_routes.endpoint_programs, methods=["GET", "POST"]),
    Route("/livetv/programs/{program_id}", live_tv_routes.endpoint_program_detail, methods=["GET"]),
    Route("/livetv/programs", live_tv_routes.endpoint_programs, methods=["GET", "POST"]),
    Route("/livetv/recordings/folders", live_tv_routes.endpoint_recordings_folders, methods=["GET"]),
    Route("/livetv/recordings", live_tv_routes.endpoint_recordings, methods=["GET"]),
    Route("/livetv/timers/defaults", live_tv_routes.endpoint_timer_defaults, methods=["GET"]),
    Route("/livetv/timers", live_tv_routes.endpoint_timers, methods=["GET"]),
    Route("/livetv/seriestimers", live_tv_routes.endpoint_series_timers, methods=["GET"]),
    Route("/livetv/channels/{channel_id}/stash-stream", live_tv_routes.endpoint_stash_channel_stream, methods=["GET"]),
    Route("/livetv/channels/{channel_id}/stash-stream.m3u8", live_tv_routes.endpoint_stash_channel_stream, methods=["GET"]),
    Route("/livetv/channels/{channel_id}/seg/{seg_name}", live_tv_routes.endpoint_stash_channel_segment, methods=["GET"]),
    Route("/livetv/channels/{channel_id}/tunarr-relay.m3u8", live_tv_routes.endpoint_tunarr_relay_stream, methods=["GET"]),
    Route("/livetv/channels/{channel_id}/stream.m3u8", live_tv_routes.endpoint_channel_m3u8, methods=["GET"]),
    Route("/livetv/channels/{channel_id}/stream", live_tv_routes.endpoint_channel_stream, methods=["GET"]),

    Route("/livestreams/open", live_tv_routes.endpoint_live_streams_open, methods=["POST"]),
    Route("/livestreams/close", live_tv_routes.endpoint_live_streams_close, methods=["POST"]),
    Route("/livestreams/ping", live_tv_routes.endpoint_live_streams_ping, methods=["POST"]),

    Route("/clientlog/document", auth_routes.endpoint_client_log, methods=["POST"]),

    WebSocketRoute("/socket", _dummy_websocket),

    Mount("/web", app=StaticFiles(directory="jellyfin-web", html=True), name="jellyfin-web"),
    Route("/{path:path}", auth_routes.endpoint_blackhole, methods=["GET", "POST", "OPTIONS", "DELETE"]),
]
