'use strict';

/**
 * channel-map.js — Singleton channel registry.
 *
 * Loads from output/channel-map.json (written by discord-setup.js on startup).
 * Reloads from disk on each access to pick up runtime changes.
 *
 * Usage:
 *   const channelMap = require('./channel-map');
 *   const ch = channelMap.getChannel(client, 'research-feed');
 *   if (ch) await ch.send('hello');
 */

const fs   = require('fs');
const path = require('path');

const WORKDIR  = process.env.OPENCLAW_DIR || '/root/openclaw';
const MAP_FILE = path.join(WORKDIR, 'output', 'channel-map.json');

class ChannelMap {
  constructor() {
    this._map      = {};
    this._loadedAt = 0;
  }

  /** Load (or reload) from disk if stale (>5s old). */
  _load() {
    const now = Date.now();
    if (now - this._loadedAt < 5000) return; // use cached version
    try {
      this._map      = JSON.parse(fs.readFileSync(MAP_FILE, 'utf8'));
      this._loadedAt = now;
    } catch { /* file may not exist yet */ }
  }

  /** Force reload from disk. */
  reload() {
    this._loadedAt = 0;
    this._load();
    return this;
  }

  /** Get a Discord channel object by key. Returns null if not in cache. */
  getChannel(client, name) {
    this._load();
    const id = this._map[name];
    if (!id) return null;
    return client.channels.cache.get(id) || null;
  }

  /** Get a channel ID by key. */
  getId(name) {
    this._load();
    return this._map[name] || null;
  }

  /** Get all channel IDs as a plain object. */
  getAll() {
    this._load();
    return { ...this._map };
  }

  /** True if the map has been successfully loaded. */
  get isReady() {
    this._load();
    return Object.keys(this._map).length > 0;
  }
}

module.exports = new ChannelMap();
