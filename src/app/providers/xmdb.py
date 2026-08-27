import logging
from datetime import timedelta

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from app import helpers
from app.models import MediaTypes, Sources
from app.providers import services

logger = logging.getLogger(__name__)
base_url = "https://xmdbapi.com/api/v1"


def _base_params():
    """Return base query params for XMDb (apiKey + lang)."""
    params = {}
    if getattr(settings, "XMDB_API", None):
        params["apiKey"] = settings.XMDB_API
    # XMDb supports lang param per-endpoint; default en if not set
    lang = getattr(settings, "XMDB_LANG", "en") or "en"
    params["lang"] = lang
    return params


def handle_error(error):
    """Handle XMDb API errors."""
    error_resp = error.response
    status_code = error_resp.status_code if error_resp else None
    try:
        error_json = error_resp.json() if error_resp else {}
    except requests.exceptions.JSONDecodeError as json_error:
        logger.exception("Failed to decode JSON response")
        raise services.ProviderAPIError(Sources.XMDB.value, error) from json_error

    if status_code == requests.codes.unauthorized:
        details = error_json.get("message") or error_json.get("error") or error_json.get("status_message")
        if details:
            details = str(details).rstrip(".")
            raise services.ProviderAPIError(Sources.XMDB.value, error, details)
    raise services.ProviderAPIError(Sources.XMDB.value, error)


def get_external_links(imdb_url=None, media_id=None):
    """Build external links for XMDb (IMDb primarily)."""
    links = {}
    if imdb_url:
        links["IMDb"] = imdb_url
    elif media_id:
        links["IMDb"] = f"https://www.imdb.com/title/{media_id}/"
    # XMDb itself is the source
    if media_id:
        links["XMDb"] = f"https://xmdbapi.com/title/{media_id}"
    return links


def search(media_type, query, page):
    """Search for media on XMDb. XMDb has single search endpoint for titles+people."""
    cache_key = f"search_{Sources.XMDB.value}_{media_type}_{query}_{page}"
    data = cache.get(cache_key)
    if data is None:
        per_page = settings.PER_PAGE  # 24
        # XMDb max limit 50; we fetch enough to paginate slice for requested page
        fetch_limit = min(page * per_page, 50)
        params = {
            **_base_params(),
            "q": query,
            "limit": fetch_limit,
        }
        try:
            response = services.api_request(
                Sources.XMDB.value,
                "GET",
                f"{base_url}/search",
                params=params,
            )
        except requests.exceptions.HTTPError as error:
            handle_error(error)

        all_results = response.get("results", [])
        # Filter to titles only (people are not tracked as TV/Movie)
        title_results = [r for r in all_results if r.get("type") == "title"]
        total_results = response.get("total", len(title_results))
        # If total includes people, adjust to title count proportionally
        # fallback: use len after filtering if total is inflated by people
        if len(all_results) != len(title_results) and total_results == len(all_results):
            total_results = len(title_results)

        # Client-side pagination slice
        start = (page - 1) * per_page
        end = start + per_page
        sliced = title_results[start:end]

        results = [
            {
                "media_id": item["id"],
                "source": Sources.XMDB.value,
                "media_type": media_type,
                "title": item.get("name") or item.get("title") or "Unknown",
                "image": item.get("image") or settings.IMG_NONE,
            }
            for item in sliced
        ]
        data = helpers.format_search_response(
            page,
            per_page,
            total_results,
            results,
        )
        cache.set(cache_key, data)
    return data


def find(external_id, external_source):
    """Find not supported for XMDb - return empty."""
    cache_key = f"find_{Sources.XMDB.value}_{external_id}_{external_source}"
    data = cache.get(cache_key)
    if data is None:
        data = {}
        cache.set(cache_key, data)
    return data


def _get_image_url(poster_url):
    if poster_url:
        return poster_url
    return settings.IMG_NONE


def _get_synopsis(plot):
    if plot and plot != "":
        return plot
    return "No synopsis available."


def _get_genres(genres):
    if genres:
        return genres
    return None


def _get_score(rating):
    if rating is None:
        return 0.0
    return round(float(rating), 1)


def _get_readable_duration(minutes):
    if minutes:
        try:
            minutes = int(minutes)
        except (ValueError, TypeError):
            return None
        hours, mins = divmod(minutes, 60)
        return f"{hours}h {mins}m" if hours > 0 else f"{mins}m"
    return None


def _get_start_date(date_obj_or_str):
    """XMDb movie.release_date can be dict with .date or string."""
    if isinstance(date_obj_or_str, dict):
        d = date_obj_or_str.get("date")
        if d and d != "":
            return d
        return None
    if isinstance(date_obj_or_str, str) and date_obj_or_str != "":
        return date_obj_or_str
    return None


def _process_movie_response(response, media_id):
    """Map XMDb movie details to Yamtrack movie structure."""
    poster = _get_image_url(response.get("poster_url"))
    title = response.get("title") or response.get("original_title") or "Unknown"
    genres = _get_genres(response.get("genres"))
    synopsis = _get_synopsis(response.get("plot"))
    score = _get_score(response.get("rating"))
    vote_count = response.get("vote_count") or 0
    runtime = _get_readable_duration(response.get("runtime_minutes"))
    release_date = _get_start_date(response.get("release_date"))
    # XMDb fields: countries, languages
    countries = response.get("countries") or []
    country = countries[0] if countries else None
    languages = response.get("languages") or None
    # production companies not available -> None
    studios = None
    status = "Released" if release_date else "Unknown"
    # cast from top_credits or full_cast
    top = response.get("top_credits", {})
    full = response.get("full_cast_and_crew", {})
    # Build cast list up to 30
    cast_src = []
    for cat in ["Stars", "Actor", "Actress"]:
        cast_src.extend(full.get(cat, [])[:10] if cat in full else [])
        cast_src.extend(top.get(cat, []) if cat in top else [])
    # dedup by id
    seen = set()
    cast = []
    for member in cast_src:
        mid = member.get("id")
        if mid in seen:
            continue
        seen.add(mid)
        cast.append({
            "id": member.get("id"),
            "name": member.get("name"),
            "character": (member.get("characters") or [""])[0] if member.get("characters") else "",
            "image": member.get("profile_image") or settings.IMG_NONE,
        })
        if len(cast) >= 30:
            break

    similar = response.get("similar_titles", [])
    related_collection = []  # XMDb has connections but not collection
    related_recs = [
        {
            "source": Sources.XMDB.value,
            "media_type": MediaTypes.MOVIE.value,
            "image": _get_image_url(item.get("poster_url")),
            "media_id": item.get("id"),
            "title": item.get("title"),
        }
        for item in similar[:20]
    ]
    external_links = get_external_links(response.get("imdb_url"), media_id)
    imdb_id = None
    if response.get("imdb_url"):
        # extract tt id
        try:
            imdb_id = response["imdb_url"].split("/title/")[1].split("/")[0]
        except Exception:
            imdb_id = None

    data = {
        "media_id": media_id,
        "source": Sources.XMDB.value,
        "source_url": response.get("imdb_url") or f"https://www.imdb.com/title/{media_id}/",
        "media_type": MediaTypes.MOVIE.value,
        "title": title,
        "max_progress": 1,
        "image": poster,
        "synopsis": synopsis,
        "genres": genres,
        "score": score,
        "score_count": vote_count,
        "details": {
            "format": "Movie",
            "release_date": release_date,
            "status": status,
            "runtime": runtime,
            "studios": studios,
            "country": country,
            "languages": languages,
        },
        "cast": cast,
        "total_cast_count": len(full.get("Actor", [])) + len(full.get("Actress", [])),
        "related": {
            "collection": related_collection,
            "recommendations": related_recs,
        },
        "external_links": external_links,
        "providers": {},  # XMDb has no watch providers
    }
    return data


def movie(media_id):
    """Return metadata for movie from XMDb."""
    cache_key = f"{Sources.XMDB.value}_{MediaTypes.MOVIE.value}_{media_id}"
    data = cache.get(cache_key)
    if data is None:
        url = f"{base_url}/movies/{media_id}"
        params = _base_params()
        try:
            response = services.api_request(
                Sources.XMDB.value,
                "GET",
                url,
                params=params,
            )
        except requests.exceptions.HTTPError as error:
            handle_error(error)
        # Guard: if title_type indicates TV, warn but still return as movie
        data = _process_movie_response(response, media_id)
        cache.set(cache_key, data)
    return data


def _fetch_seasons_overview(media_id):
    """Fetch seasons overview for a series."""
    url = f"{base_url}/seasons/{media_id}"
    params = _base_params()
    try:
        return services.api_request(Sources.XMDB.value, "GET", url, params=params)
    except requests.exceptions.HTTPError as error:
        handle_error(error)


def _fetch_season_episodes(media_id, season_number):
    """Fetch episodes for a specific season."""
    url = f"{base_url}/seasons/{media_id}"
    params = {**_base_params(), "season": int(season_number)}
    try:
        return services.api_request(Sources.XMDB.value, "GET", url, params=params)
    except requests.exceptions.HTTPError as error:
        handle_error(error)


def _build_tv_data(base_response, seasons_overview, seasons_detail_map):
    """Build tv dict similar to tmdb.process_tv."""
    title = base_response.get("title") or base_response.get("original_title") or "Unknown"
    poster = _get_image_url(base_response.get("poster_url"))
    genres = _get_genres(base_response.get("genres"))
    synopsis = _get_synopsis(base_response.get("plot"))
    score = _get_score(base_response.get("rating"))
    vote_count = base_response.get("vote_count") or 0
    # first air date from first episode of first season or release_date
    first_air = _get_start_date(base_response.get("release_date"))
    last_air = None
    # Try to get dates from seasons_detail_map
    if seasons_detail_map:
        # sorted season numbers
        for sn in sorted(seasons_detail_map.keys()):
            eps = seasons_detail_map[sn].get("episodes", [])
            if eps:
                if not first_air:
                    first_air = eps[0].get("release_date")
                last_air = eps[-1].get("release_date")
    # status based on is_ongoing
    is_ongoing = seasons_overview.get("is_ongoing")
    if is_ongoing is True:
        status = "Returning Series"
    elif is_ongoing is False:
        status = "Ended"
    else:
        status = "Unknown"
    season_count = seasons_overview.get("season_count") or len(seasons_overview.get("seasons", []))
    # count episodes
    total_episodes = sum(len(v.get("episodes", [])) for v in seasons_detail_map.values()) if seasons_detail_map else 0
    if total_episodes == 0 and season_count:
        # fallback: if we didn't fetch details, estimate
        total_episodes = 0
    runtime = _get_readable_duration(base_response.get("runtime_minutes"))
    countries = base_response.get("countries") or []
    country = countries[0] if countries else None
    languages = base_response.get("languages") or None
    studios = None
    # related seasons
    related_seasons = []
    for sn in sorted(seasons_detail_map.keys()):
        detail = seasons_detail_map[sn]
        eps = detail.get("episodes", [])
        season_title = f"Season {sn}"
        first_ep_date = eps[0].get("release_date") if eps else None
        image = poster  # XMDb episodes have image_url per ep, but season poster fallback to show poster
        related_seasons.append({
            "source": Sources.XMDB.value,
            "media_type": MediaTypes.SEASON.value,
            "image": image,
            "media_id": base_response.get("id"),
            "title": title,
            "season_number": int(sn),
            "season_title": season_title,
            "first_air_date": first_ep_date,
            "max_progress": len(eps),
        })
    # If seasons_detail_map empty (no detail fetched), build from overview seasons list
    if not related_seasons and seasons_overview.get("seasons"):
        for s in seasons_overview["seasons"]:
            try:
                sn = int(s)
            except ValueError:
                continue
            related_seasons.append({
                "source": Sources.XMDB.value,
                "media_type": MediaTypes.SEASON.value,
                "image": poster,
                "media_id": base_response.get("id"),
                "title": title,
                "season_number": sn,
                "season_title": f"Season {sn}",
                "first_air_date": first_air,
                "max_progress": 0,
            })
    similar = base_response.get("similar_titles", [])
    related_recs = [
        {
            "source": Sources.XMDB.value,
            "media_type": MediaTypes.TV.value,
            "image": _get_image_url(item.get("poster_url")),
            "media_id": item.get("id"),
            "title": item.get("title"),
        }
        for item in similar[:20]
    ]
    data = {
        "media_id": base_response.get("id"),
        "source": Sources.XMDB.value,
        "source_url": base_response.get("imdb_url") or f"https://www.imdb.com/title/{base_response.get('id')}/",
        "media_type": MediaTypes.TV.value,
        "title": title,
        "max_progress": total_episodes or season_count,  # fallback to season_count if eps unknown
        "image": poster,
        "synopsis": synopsis,
        "genres": genres,
        "score": score,
        "score_count": vote_count,
        "details": {
            "format": "TV",
            "first_air_date": first_air,
            "last_air_date": last_air,
            "status": status,
            "seasons": season_count,
            "episodes": total_episodes,
            "runtime": runtime,
            "studios": studios,
            "country": country,
            "languages": languages,
        },
        "related": {
            "seasons": related_seasons,
            "recommendations": related_recs,
        },
        "tvdb_id": None,
        "external_links": get_external_links(base_response.get("imdb_url"), base_response.get("id")),
        "last_episode_season": None,
        "next_episode_season": None,
        "providers": {},
    }
    # provide accurate max_progress as total episodes
    if total_episodes:
        data["max_progress"] = total_episodes
    return data


def tv(media_id):
    """Return TV show metadata from XMDb."""
    cache_key = f"{Sources.XMDB.value}_{MediaTypes.TV.value}_{media_id}"
    data = cache.get(cache_key)
    if data is None:
        # base show details
        url = f"{base_url}/movies/{media_id}"
        params = _base_params()
        try:
            base_resp = services.api_request(Sources.XMDB.value, "GET", url, params=params)
        except requests.exceptions.HTTPError as error:
            handle_error(error)
        # seasons overview
        try:
            seasons_overview = _fetch_seasons_overview(media_id)
        except services.ProviderAPIError:
            # If not a TV series (movie), fallback to minimal tv data without seasons
            seasons_overview = {"season_count": 0, "seasons": [], "is_ongoing": None}
        # fetch details for all seasons to build accurate counts (limit to avoid huge)
        seasons_detail_map = {}
        for s in seasons_overview.get("seasons", [])[:20]:  # safety cap
            try:
                sn = int(s)
            except ValueError:
                continue
            try:
                detail = _fetch_season_episodes(media_id, sn)
                seasons_detail_map[sn] = detail
            except Exception as e:
                logger.warning("Failed to fetch xmdb season %s for %s: %s", sn, media_id, e)
        data = _build_tv_data(base_resp, seasons_overview, seasons_detail_map)
        cache.set(cache_key, data)
    return data


def _process_season_response(season_detail, base_response, season_number):
    """Process XMDb season detail into Yamtrack season structure."""
    episodes = season_detail.get("episodes", [])
    num_eps = len(episodes)
    score = None
    vote_total = 0
    runtimes = []
    total_runtime = 0
    for ep in episodes:
        vote_total += ep.get("vote_count") or 0
        rt = ep.get("runtime_minutes")
        if rt:
            runtimes.append(rt)
            total_runtime += rt
        if ep.get("rating"):
            score = ep.get("rating")
    # average rating for season; fallback to show rating
    avg_score = score if score is not None else (base_response.get("rating") or 0)
    avg_runtime = _get_readable_duration(sum(runtimes)/len(runtimes)) if runtimes else None
    total_runtime_str = _get_readable_duration(total_runtime) if total_runtime else None
    first_air = episodes[0].get("release_date") if episodes else None
    last_air = episodes[-1].get("release_date") if episodes else None
    # synopsis from first episode plot or show plot
    synopsis = base_response.get("plot") or "No synopsis available."
    if episodes and episodes[0].get("plot"):
        synopsis = episodes[0].get("plot")
    if synopsis == "":
        synopsis = "No synopsis available."

    # Transform episodes to tmdb-like structure for process_episodes
    tmdb_like_eps = []
    for ep in episodes:
        tmdb_like_eps.append({
            "id": ep.get("id"),
            "episode_number": ep.get("episode_number"),
            "season_number": ep.get("season_number"),
            "name": ep.get("title"),
            "overview": ep.get("plot") or "",
            "air_date": ep.get("release_date"),
            "runtime": ep.get("runtime_minutes"),
            "vote_average": ep.get("rating") or 0,
            "vote_count": ep.get("vote_count") or 0,
            "still_path": None,  # will be handled via image fallback
            "image_url": ep.get("image_url"),
        })

    return {
        "source": Sources.XMDB.value,
        "media_type": MediaTypes.SEASON.value,
        "season_title": f"Season {season_number}",
        "max_progress": tmdb_like_eps[-1]["episode_number"] if tmdb_like_eps else 0,
        "image": _get_image_url(base_response.get("poster_url")),
        "season_number": int(season_number),
        "synopsis": _get_synopsis(synopsis),
        "score": _get_score(avg_score),
        "score_count": vote_total,
        "details": {
            "first_air_date": first_air,
            "last_air_date": last_air,
            "episodes": num_eps,
            "runtime": avg_runtime,
            "total_runtime": total_runtime_str,
        },
        "episodes": tmdb_like_eps,
        "providers": {},
    }


def tv_with_seasons(media_id, season_numbers):
    """Return TV show with seasons appended."""
    if not season_numbers:
        return tv(media_id)
    tv_cache_key = f"{Sources.XMDB.value}_{MediaTypes.TV.value}_{media_id}"
    tv_data = cache.get(tv_cache_key)
    # Check cache for each season individually (mirrors tmdb logic)
    cached = {}
    uncached = []
    for sn in season_numbers:
        sk = f"{Sources.XMDB.value}_{MediaTypes.SEASON.value}_{media_id}_{sn}"
        sd = cache.get(sk)
        if sd:
            cached[f"season/{sn}"] = sd
        else:
            uncached.append(sn)

    # Need base response for enriching
    if tv_data is None and not uncached:
        tv_data = tv(media_id)

    if uncached:
        # fetch base if missing
        if tv_data is None:
            # fetch base response for enriching season data
            try:
                base_resp = services.api_request(Sources.XMDB.value, "GET", f"{base_url}/movies/{media_id}", params=_base_params())
            except requests.exceptions.HTTPError as error:
                handle_error(error)
            seasons_overview = _fetch_seasons_overview(media_id)
            # build minimal tv_data without waiting for all seasons
            tv_data = _build_tv_data(base_resp, seasons_overview, {})
            cache.set(tv_cache_key, tv_data)
            base_for_seasons = base_resp
        else:
            # get base from tv_data? Need to refetch for image/synopsis; use cached movie details if available
            # We can fetch again to ensure base
            try:
                base_for_seasons = services.api_request(Sources.XMDB.value, "GET", f"{base_url}/movies/{media_id}", params=_base_params())
            except Exception:
                base_for_seasons = {"title": tv_data.get("title"), "poster_url": tv_data.get("image"), "plot": tv_data.get("synopsis"), "rating": tv_data.get("score")}

        for sn in uncached:
            detail = _fetch_season_episodes(media_id, sn)
            season_data = _process_season_response(detail, base_for_seasons, sn)
            # enrich with tv data similar to tmdb.enrich_season_with_tv_data
            season_data["media_id"] = media_id
            season_data["source_url"] = f"https://www.imdb.com/title/{media_id}/"
            season_data["title"] = tv_data.get("title")
            season_data["tvdb_id"] = None
            season_data["external_links"] = tv_data.get("external_links", {})
            season_data["genres"] = tv_data.get("genres")
            # fallback image if season has none
            if season_data["image"] == settings.IMG_NONE:
                season_data["image"] = tv_data.get("image")
            cache.set(f"{Sources.XMDB.value}_{MediaTypes.SEASON.value}_{media_id}_{sn}", season_data)
            cached[f"season/{sn}"] = season_data

        # If we created tv_data earlier via _build, ensure it's updated with correct episode counts
        # Re-fetch tv_data to refresh max_progress if needed
        # Not strictly needed

    return tv_data | cached


def process_episodes(season_metadata, episodes_in_db):
    """Process episodes for season display. Mirrors tmdb.process_episodes but supports xmdb image_url."""
    episodes_metadata = []
    tracked = {}
    for ep in episodes_in_db:
        en = ep.item.episode_number
        tracked.setdefault(en, []).append(ep)

    for episode in season_metadata["episodes"]:
        episode_number = episode["episode_number"]
        # image handling: xmdb stores image_url, tmdb uses still_path
        img = episode.get("image_url") or episode.get("still_path")
        if img and img.startswith("http"):
            image = img
        elif episode.get("still_path"):
            image = f"https://image.tmdb.org/t/p/w500{episode['still_path']}"
        else:
            image = settings.IMG_NONE

        episodes_metadata.append({
            "media_id": season_metadata["media_id"],
            "media_type": MediaTypes.EPISODE.value,
            "source": Sources.XMDB.value,
            "season_number": season_metadata["season_number"],
            "episode_number": episode_number,
            "air_date": episode.get("air_date"),
            "image": image,
            "title": episode.get("name") or episode.get("title") or f"Episode {episode_number}",
            "overview": episode.get("overview") or "",
            "history": tracked.get(episode_number, []),
            "runtime": _get_readable_duration(episode.get("runtime")),
            "runtime_minutes": episode.get("runtime"),
        })
    return episodes_metadata


def find_next_episode(episode_number, episodes_metadata):
    """Find next episode number (shared logic)."""
    current_idx = None
    for idx, ep in enumerate(episodes_metadata):
        if ep["episode_number"] == episode_number:
            current_idx = idx
            break
    if current_idx is None or current_idx + 1 >= len(episodes_metadata):
        return None
    return episodes_metadata[current_idx + 1]["episode_number"]


def episode(media_id, season_number, episode_number):
    """Return metadata for single episode."""
    tv_metadata = tv_with_seasons(media_id, [season_number])
    season_metadata = tv_metadata.get(f"season/{season_number}")
    if not season_metadata:
        msg = f"Season {season_number} not found for {Sources.XMDB.label} with ID {media_id}"
        not_found = requests.Response()
        not_found.status_code = 404
        err = type("Error", (), {"response": not_found})
        raise services.ProviderAPIError(Sources.XMDB.value, error=err, details=msg)
    for ep in season_metadata["episodes"]:
        if ep["episode_number"] == int(episode_number):
            img = ep.get("image_url") or ep.get("still_path")
            if img and img.startswith("http"):
                image = img
            elif ep.get("still_path"):
                image = f"https://image.tmdb.org/t/p/w500{ep['still_path']}"
            else:
                image = settings.IMG_NONE
            return {
                "title": season_metadata.get("title"),
                "season_title": season_metadata.get("season_title"),
                "episode_title": ep.get("name") or ep.get("title"),
                "image": image,
            }
    msg = f"Episode {episode_number} not found in season {season_number} for {Sources.XMDB.label} with ID {media_id}"
    not_found = requests.Response()
    not_found.status_code = 404
    err = type("Error", (), {"response": not_found})
    raise services.ProviderAPIError(Sources.XMDB.value, error=err, details=msg)


def filter_providers(all_providers, region):
    """XMDb has no watch providers."""
    return [] if all_providers == {} else None if region == "" else []


def watch_provider_regions():
    """Return disabled only for XMDb."""
    return [("", "Disabled")]


def get_changed_ids(media_type):
    """XMDb upcoming could be used but return empty for now."""
    return set()


def tv_changes():
    return get_changed_ids(MediaTypes.TV.value)


def movie_changes():
    return get_changed_ids(MediaTypes.MOVIE.value)


def get_image_url(path_or_url):
    """For compatibility, return URL directly if already http."""
    if path_or_url and str(path_or_url).startswith("http"):
        return path_or_url
    if path_or_url:
        return f"https://image.tmdb.org/t/p/w500{path_or_url}"
    return settings.IMG_NONE

