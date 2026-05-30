'use strict';
const assert = require('assert');
const router = require('../src/channels/api/routes_regime_proposals.js');
const layers = (router.stack || []).filter(l => l.route && l.route.path === '/applied');
assert.strictEqual(layers.length, 1, 'GET /applied route registered');
assert(layers[0].route.methods.get, '/applied is a GET');
console.log('OK test_applied_route');
