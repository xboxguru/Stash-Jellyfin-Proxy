import re
import json
import datetime
from starlette.requests import Request
import config
from core import stash_client
from core.jellyfin_mapper import decode_id

def transform_saved_filter(object_filter: dict) -> dict:
    """Convert a Stash saved-filter object_filter blob into a SceneFilterType-compatible dict."""
    if not object_filter or not isinstance(object_filter, dict):
        return {}
    result = {}
    for key, value in object_filter.items():
        if value is None:
            continue
        if key in ('has_markers', 'is_missing'):
            result[key] = str(value['value']).lower() if isinstance(value, dict) and 'value' in value else str(value).lower()
            continue
        # Stash GraphQL expects these as plain Booleans, not filter objects
        if key in ('organized', 'interactive'):
            if isinstance(value, bool):
                result[key] = value
            elif isinstance(value, dict) and 'value' in value:
                result[key] = bool(value['value'])
            continue
        if key in ('AND', 'OR', 'NOT'):
            if isinstance(value, list):
                result[key] = [transform_saved_filter(v) for v in value if v]
            elif isinstance(value, dict):
                result[key] = transform_saved_filter(value)
            continue
        if isinstance(value, dict):
            modifier = value.get('modifier')
            val = value.get('value')
            if 'items' in value:
                ids = [item.get('id') for item in value['items'] if isinstance(item, dict) and item.get('id')]
                excludes = [e.get('id') if isinstance(e, dict) else e for e in value.get('excluded', [])]
                result[key] = {'value': ids, 'modifier': modifier, 'depth': value.get('depth', 0), 'excludes': excludes}
                continue
            if modifier in ('IS_NULL', 'NOT_NULL'):
                result[key] = {'value': '', 'modifier': modifier}
                continue
            if isinstance(val, dict) and 'value' in val:
                val = val['value']
            if modifier and val is not None:
                transformed = {'modifier': modifier, 'value': val}
                for k, v in value.items():
                    if k not in ('modifier', 'value'):
                        transformed[k] = v
                result[key] = transformed
                continue
        result[key] = value
    return result


class StashQueryBuilder:
    """Translates Jellyfin UI requests into Stash GraphQL filter payloads."""
    
    def __init__(self, request: Request, query_data: dict):
        self.request = request
        self.q = query_data
        self.filter_args = {"sort": "created_at", "direction": "DESC"}
        self.scene_filter = {}
        self.is_folder_override = False
        self.limit = self.q.get("limit", getattr(config, "DEFAULT_PAGE_SIZE", 50))

    def _get_query_param(self, param_name: str, default=""):
        values = [value for key, value in self.request.query_params.multi_items() if key.lower() == param_name.lower()]
        return ",".join(values) if values else default

    def _transform_saved_filter(self, object_filter):
        return transform_saved_filter(object_filter)

    async def build(self) -> tuple[dict, dict, bool, int]:
        sync_mode = getattr(config, "SYNC_LEVEL", "Everything")
        if sync_mode == "Organized": self.scene_filter["organized"] = True
        elif sync_mode == "Tagged": self.scene_filter["tags"] = {"modifier": "NOT_NULL"}
        
        decoded_parent_id = self.q.get("decoded_parent_id")
        if decoded_parent_id:
            if decoded_parent_id == "root-scenes": 
                self.is_folder_override = True
            elif decoded_parent_id == "root-organized": 
                self.scene_filter["organized"], self.is_folder_override = True, True
            elif decoded_parent_id == "root-tagged": 
                self.scene_filter["tags"], self.is_folder_override = {"modifier": "NOT_NULL"}, True
            elif decoded_parent_id == "root-recent":
                cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=getattr(config, "RECENT_DAYS", 14))).strftime("%Y-%m-%dT%H:%M:%S")
                self.scene_filter["created_at"], self.is_folder_override = {"value": cutoff, "modifier": "GREATER_THAN"}, True
            elif decoded_parent_id.startswith("tag-"): 
                self.scene_filter["tags"], self.is_folder_override = {"value": [decoded_parent_id.replace("tag-", "")], "modifier": "INCLUDES"}, True
            elif decoded_parent_id.startswith("person-"):
                self.scene_filter["performers"], self.is_folder_override = {"value": [decoded_parent_id.replace("person-", "")], "modifier": "INCLUDES"}, True
            elif decoded_parent_id.startswith("studio-"):
                self.scene_filter["studios"], self.is_folder_override = {"value": [decoded_parent_id.replace("studio-", "")], "modifier": "INCLUDES"}, True
            elif decoded_parent_id.startswith("filter-"):
                self.is_folder_override = True
                raw_filter_id = decoded_parent_id.replace("filter-", "")
                filters = await stash_client.get_saved_filters()
                data = next((f for f in filters if str(f.get("id")) == raw_filter_id), None)
                if data:
                    if data.get("object_filter"): self.scene_filter.update(self._transform_saved_filter(data["object_filter"]))
                    elif data.get("filter"):
                        parsed = json.loads(data["filter"])
                        if "scene_filter" in parsed: self.scene_filter.update(self._transform_saved_filter(parsed["scene_filter"]))
                        if "q" in parsed: self.filter_args["q"] = parsed["q"]
                        if "sort" in parsed: self.filter_args["sort"] = parsed["sort"]
                        if "direction" in parsed: self.filter_args["direction"] = parsed["direction"]

        person_ids = self.q.get("person_ids")
        if person_ids:
            raw_p_ids = [m.group() for p in person_ids.split(",") if (m := re.search(r'\d+', decode_id(p)))]
            if raw_p_ids: self.scene_filter["performers"] = {"value": raw_p_ids, "modifier": "INCLUDES"}

        sort_by = self._get_query_param("SortBy").split(",")[0].lower()
        if "random" in sort_by: self.filter_args["sort"] = "random"
        elif "datecreated" in sort_by: self.filter_args["sort"] = "created_at"
        elif "dateplayed" in sort_by: self.filter_args["sort"] = "updated_at" 
        elif "name" in sort_by or "sortname" in sort_by: self.filter_args["sort"] = "title"

        sort_order = self._get_query_param("SortOrder").split(",")[0].lower()
        if sort_order == "ascending": self.filter_args["direction"] = "ASC"
        else: self.filter_args["direction"] = "DESC"

        filters_string = self.q.get("filters_string")
        filter_list = [f.strip() for f in filters_string.split(",")] if filters_string else []
        is_fav = self._get_query_param("isFavorite").lower()
        is_play = self._get_query_param("isPlayed").lower()
        
        if "IsFavorite" in filter_list or is_fav == "true": self.scene_filter["o_counter"] = {"value": 0, "modifier": "GREATER_THAN"}
        elif is_fav == "false": self.scene_filter["o_counter"] = {"value": 0, "modifier": "EQUALS"}
            
        if "IsUnplayed" in filter_list or is_play == "false": self.scene_filter["play_count"] = {"value": 0, "modifier": "EQUALS"}
        elif "IsPlayed" in filter_list or is_play == "true": self.scene_filter["play_count"] = {"value": 0, "modifier": "GREATER_THAN"}

        if "IsResumable" in filter_list: 
            self.filter_args["sort"], self.filter_args["direction"], self.limit = "updated_at", "DESC", 100 

        years = self._get_query_param("Years")
        if years:
            y_l = [int(m.group()) for y in years.split(",") if (m := re.search(r'\d{4}', decode_id(y)))]
            if y_l: self.scene_filter["date"] = {"value": f"{min(y_l)}-01-01", "value2": f"{max(y_l)}-12-31", "modifier": "BETWEEN"}

        tags_param = self.q.get("tags_param")
        if tags_param:
            raw_t = [m.group() for t in tags_param.split(",") if (m := re.search(r'\d+', decode_id(t)))]
            if raw_t: self.scene_filter["tags"] = {"value": raw_t, "modifier": "INCLUDES"}

        studio_ids_param = self.q.get("studio_ids_param")
        if studio_ids_param:
            raw_s_ids = [m.group() for s in studio_ids_param.split(",") if (m := re.search(r'\d+', decode_id(s)))]
            if raw_s_ids: self.scene_filter["studios"] = {"value": raw_s_ids, "modifier": "INCLUDES"}

        search_term = self.q.get("search_term")
        if search_term: self.filter_args["q"] = search_term
        
        return self.filter_args, self.scene_filter, self.is_folder_override, self.limit