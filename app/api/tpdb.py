"""
ThePornDB API client for fetching adult content metadata.
Documentation: https://theporndb.net/docs/api
"""

import os
import httpx
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime


API_BASE = "https://api.theporndb.net"
IMAGE_BASE = "https://cdn.theporndb.net"


@dataclass
class TPDBScene:
    """Scene metadata from ThePornDB."""
    id: str
    title: str
    site: str
    network: Optional[str] = None
    performers: list[str] = field(default_factory=list)
    release_date: Optional[str] = None  # YYYY-MM-DD
    duration: Optional[int] = None  # seconds
    tags: list[str] = field(default_factory=list)
    poster_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    description: Optional[str] = None

    @property
    def poster_url_large(self) -> Optional[str]:
        if self.poster_url:
            return f"{IMAGE_BASE}{self.poster_url}"
        return None

    @property
    def thumbnail_url_small(self) -> Optional[str]:
        if self.thumbnail_url:
            return f"{IMAGE_BASE}{self.thumbnail_url}"
        return None


@dataclass
class TPDBPerformer:
    """Performer metadata from ThePornDB."""
    id: str
    name: str
    aliases: list[str] = field(default_factory=list)
    image_url: Optional[str] = None
    bio: Optional[str] = None


@dataclass
class TPDBSite:
    """Site/studio metadata from ThePornDB."""
    id: str
    name: str
    network: Optional[str] = None
    url: Optional[str] = None
    logo_url: Optional[str] = None


def _classify_http_error(e: Exception) -> str:
    """Map an httpx error to a user-meaningful kind: auth | rate_limit | network.

    Lets the caller distinguish "the provider call failed" from "the scene is
    not in the database" (F15) — without ever surfacing raw exception text
    (which can echo URLs/headers) to the client.
    """
    resp = getattr(e, "response", None)
    if resp is not None:
        if resp.status_code in (401, 403):
            return "auth"
        if resp.status_code == 429:
            return "rate_limit"
    return "network"


class TPDBClient:
    """
    ThePornDB API client.

    Requires TPDB_API_KEY environment variable.
    """

    def __init__(self, api_key: Optional[str] = None):
        """Initialize TPDB client with API key."""
        self.api_key = api_key or os.getenv("TPDB_API_KEY")
        # Kind of the most recent lookup failure (auth/rate_limit/network),
        # None after a clean call. Reset on ENTRY to each lookup (not "cleared
        # after read") so a stale flag can't mislabel a later success; with
        # concurrent lookups a cross-task read is possible but harmless — the
        # classified conditions (bad key, rate limit) are global anyway (F15).
        self.last_error: Optional[str] = None
        if not self.api_key:
            raise ValueError(
                "TPDB API key required. Set TPDB_API_KEY environment variable "
                "or pass api_key parameter. Get your key at https://theporndb.net/"
            )
        
        self._client = httpx.AsyncClient(
            base_url=API_BASE,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "User-Agent": "Adult-Media-Manager/1.0"
            },
            timeout=30.0,
        )
    
    async def close(self):
        """Close the HTTP client."""
        await self._client.aclose()
    
    async def search_scene(
        self, 
        query: str, 
        site: Optional[str] = None,
        performer: Optional[str] = None
    ) -> list[TPDBScene]:
        """
        Search for scenes by query.
        
        Args:
            query: Search query (scene title, filename)
            site: Filter by site name
            performer: Filter by performer name
            
        Returns:
            List of matching scenes
        """
        params = {"q": query}
        if site:
            params["site"] = site
        if performer:
            params["performer"] = performer

        self.last_error = None
        try:
            resp = await self._client.get("/scenes", params=params)
            resp.raise_for_status()
            data = resp.json()
            
            results = []
            for item in data.get("data", []):
                results.append(self._parse_scene(item))
            
            return results
        except httpx.HTTPError as e:
            self.last_error = _classify_http_error(e)
            print(f"TPDB API error ({self.last_error}): {e}")
            return []
    
    async def parse_filename(self, filename: str) -> Optional[TPDBScene]:
        """
        Parse filename and search for matching scene.
        
        TPDB has intelligent filename parsing that can extract
        site, performer, date, and scene title from filenames.
        
        Args:
            filename: Filename to parse
            
        Returns:
            Best matching scene or None
        """
        params = {"parse": filename}

        self.last_error = None
        try:
            resp = await self._client.get("/scenes", params=params)
            resp.raise_for_status()
            data = resp.json()
            
            items = data.get("data", [])
            if items:
                return self._parse_scene(items[0])
            return None
        except httpx.HTTPError as e:
            self.last_error = _classify_http_error(e)
            print(f"TPDB parse error ({self.last_error}): {e}")
            return None
    
    async def get_scene(self, scene_id: str) -> Optional[TPDBScene]:
        """
        Get scene details by ID.
        
        Args:
            scene_id: TPDB scene ID
            
        Returns:
            Scene details or None
        """
        try:
            resp = await self._client.get(f"/scenes/{scene_id}")
            resp.raise_for_status()
            data = resp.json()
            return self._parse_scene(data.get("data", {}))
        except httpx.HTTPError as e:
            print(f"TPDB get scene error: {e}")
            return None
    
    async def search_performer(self, name: str) -> list[TPDBPerformer]:
        """
        Search for performers by name.
        
        Args:
            name: Performer name
            
        Returns:
            List of matching performers
        """
        params = {"q": name}
        
        try:
            resp = await self._client.get("/performers", params=params)
            resp.raise_for_status()
            data = resp.json()
            
            results = []
            for item in data.get("data", []):
                results.append(self._parse_performer(item))
            
            return results
        except httpx.HTTPError as e:
            print(f"TPDB performer search error: {e}")
            return []
    
    async def search_sites(self, query: str) -> list["TPDBSite"]:
        """Search for sites/studios by name."""
        params = {"q": query}
        try:
            resp = await self._client.get("/sites", params=params)
            resp.raise_for_status()
            data = resp.json()
            return [self._parse_site(item) for item in data.get("data", [])[:20]]
        except httpx.HTTPError as e:
            print(f"TPDB site search error: {e}")
            return []

    async def get_site(self, site_id: str) -> Optional[TPDBSite]:
        """
        Get site details by ID.
        
        Args:
            site_id: TPDB site ID
            
        Returns:
            Site details or None
        """
        try:
            resp = await self._client.get(f"/sites/{site_id}")
            resp.raise_for_status()
            data = resp.json()
            return self._parse_site(data.get("data", {}))
        except httpx.HTTPError as e:
            print(f"TPDB get site error: {e}")
            return None
    
    def _parse_scene(self, data: dict) -> TPDBScene:
        """Parse scene data from API response."""
        performers = []
        for p in data.get("performers", []):
            if isinstance(p, dict):
                performers.append(p.get("name", ""))
            else:
                performers.append(str(p))
        
        tags = []
        for t in data.get("tags", []):
            if isinstance(t, dict):
                tags.append(t.get("name", ""))
            else:
                tags.append(str(t))
        
        site_data = data.get("site", {})
        site_name = site_data.get("name", "") if isinstance(site_data, dict) else str(site_data)

        # Normalise network to a plain string — the API can return a dict
        # ({"name": "MindGeek", "id": 123}) which would produce dirty filenames
        # if passed directly into template rendering.
        network_raw = site_data.get("network") if isinstance(site_data, dict) else None
        if isinstance(network_raw, dict):
            network_str: Optional[str] = (
                network_raw.get("name") or network_raw.get("short_name") or None
            )
        elif network_raw:
            network_str = str(network_raw)
        else:
            network_str = None

        return TPDBScene(
            id=str(data.get("id", "")),
            title=data.get("title", ""),
            site=site_name,
            network=network_str,
            performers=performers,
            release_date=data.get("date", ""),
            duration=data.get("duration"),
            tags=tags,
            poster_url=data.get("poster"),
            thumbnail_url=data.get("background") or data.get("poster"),
            description=data.get("description", ""),
        )
    
    def _parse_performer(self, data: dict) -> TPDBPerformer:
        """Parse performer data from API response."""
        aliases = data.get("aliases", [])
        if isinstance(aliases, str):
            aliases = [aliases]
        
        return TPDBPerformer(
            id=str(data.get("id", "")),
            name=data.get("name", ""),
            aliases=aliases,
            image_url=data.get("image"),
            bio=data.get("bio"),
        )
    
    def _parse_site(self, data: dict) -> TPDBSite:
        """Parse site data from API response."""
        network_raw = data.get("network")
        if isinstance(network_raw, dict):
            network_name = network_raw.get("name") or network_raw.get("short_name")
        else:
            network_name = str(network_raw) if network_raw else None
        return TPDBSite(
            id=str(data.get("id", "")),
            name=data.get("name", ""),
            network=network_name,
            url=data.get("url"),
            logo_url=data.get("logo"),
        )
