// ==UserScript==
// @name         Travian Instant Video Reward v5
// @namespace    openclaw
// @version      5.0
// @description  Simulates video playback via ATG API calls - gets rewards without watching videos
// @match        https://*.travian.com/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==

(function () {
  'use strict';

  const TAG = '[⚡ v5]';
  const log = (...a) => console.log(TAG, ...a);
  const warn = (...a) => console.warn(TAG, ...a);

  log('=== Travian Instant Video Reward v5 ===');
  log('Intercepts video iframe, simulates playback via ATG API, gets hash instantly.');

  // ── Intercept the postMessage listener to inject our own flow ──

  // Store original iframe creation
  const origCreateElement = document.createElement.bind(document);

  // Intercept fetch/XHR to capture the /videofeature/open/ response
  const _fetch = window.fetch;
  window.fetch = function (...args) {
    const url = typeof args[0] === 'string' ? args[0] : args[0]?.url || '';

    if (url.includes('/videofeature/open/')) {
      log('🎯 Intercepted videofeature/open call:', url);
      return _fetch.apply(this, args).then(async (resp) => {
        const clone = resp.clone();
        try {
          const data = await clone.json();
          log('📦 Video open response:', JSON.stringify(data));
          if (data.vrid && data.videoIframeUrl) {
            // Start the simulated playback
            simulateVideoPlayback(data.vrid, data.videoIframeUrl);
          }
        } catch (e) {
          warn('Could not parse open response:', e);
        }
        return resp;
      });
    }

    return _fetch.apply(this, args);
  };

  // Also intercept XHR
  const _xhrOpen = XMLHttpRequest.prototype.open;
  const _xhrSend = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function (method, url) {
    this._url = url;
    this._method = method;
    return _xhrOpen.apply(this, arguments);
  };

  XMLHttpRequest.prototype.send = function (body) {
    if (this._url?.includes('/videofeature/open/')) {
      log('🎯 Intercepted XHR videofeature/open:', this._url);
      const origOnLoad = this.onload;
      const origOnReady = this.onreadystatechange;

      this.addEventListener('load', () => {
        try {
          const data = JSON.parse(this.responseText);
          log('📦 XHR Video open response:', JSON.stringify(data));
          if (data.vrid && data.videoIframeUrl) {
            simulateVideoPlayback(data.vrid, data.videoIframeUrl);
          }
        } catch (e) { }
      });
    }
    return _xhrSend.apply(this, arguments);
  };

  // ── Core: Simulate video playback via ATG APIs ──

  async function simulateVideoPlayback(vrid, iframeUrl) {
    log('🚀 Starting simulated playback for vrid:', vrid);

    // Step 1: Fetch the iframe HTML to get the ATG config
    const fullUrl = iframeUrl.startsWith('//') ? 'https:' + iframeUrl : iframeUrl;

    try {
      const resp = await fetch(fullUrl);
      const html = await resp.text();

      // Extract the base64 config
      const b64Match = html.match(/atob\("([^"]+)"\)/);
      if (!b64Match) {
        // Try alternate pattern
        const b64Match2 = html.match(/atob\('([^']+)'\)/);
        if (!b64Match2) {
          warn('❌ Could not find ATG config in iframe HTML');
          return;
        }
        b64Match = b64Match2;
      }

      const config = JSON.parse(atob(b64Match[1]));
      log('✅ Got ATG config:', JSON.stringify(config.xsign ? 'xsign present' : 'no xsign'));

      if (!config.xsign) {
        warn('❌ No xsign in config, cannot simulate');
        return;
      }

      const xsign = config.xsign;
      let xc = xsign.xc;
      const fcUrl = xsign.fc;
      const xsUrl = xsign.xs;

      // Get a banner ID and zone ID from the waterfall
      const b = config.waterfall?.[0]?.bid || '17606';
      const z = config.zone_id || 3716;

      log('📋 Config: fc=' + fcUrl + ' xs=' + xsUrl + ' b=' + b + ' z=' + z);
      log('📋 Initial xc:', JSON.stringify(xc));

      // Step 2: Simulate time ticks (video progress)
      // The onTime logic: every 3 seconds, send progress to fc.php
      // We'll simulate a 30-second video in rapid fire
      const totalDuration = 30;
      const tickInterval = 3;

      for (let ts = 0; ts <= totalDuration; ts += tickInterval) {
        const remaining = totalDuration - ts;

        log(`⏱️ Tick: ${ts}s / ${totalDuration}s (remaining: ${remaining}s)`);

        try {
          const payload = JSON.stringify({
            self: xc,
            at: ts,
            rm: remaining,
            b: String(b),
            z: String(z)
          });

          const fcResp = await fetch(fcUrl, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: payload
          });

          if (fcResp.ok) {
            const newXc = await fcResp.json();
            xc = newXc;
            log(`✅ fc.php response at ${ts}s:`, JSON.stringify(xc));
          } else {
            warn(`⚠️ fc.php returned ${fcResp.status} at ${ts}s`);
          }
        } catch (e) {
          warn(`⚠️ fc.php error at ${ts}s:`, e);
        }

        // Small delay between ticks to not look suspicious (50ms instead of 3s)
        await new Promise(r => setTimeout(r, 50));
      }

      // Step 3: Call xs.php to get the signature
      log('🔐 Requesting signature from xs.php...');

      const xsPayload = JSON.stringify({
        self: xc,
        csid: b + '-' + z,
        val: 2
      });

      const xsResp = await fetch(xsUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: xsPayload
      });

      if (!xsResp.ok) {
        warn('❌ xs.php returned', xsResp.status);
        return;
      }

      const sign = await xsResp.text();
      log('🔑 Got signature:', sign);

      // Step 4: Post the videoEnds message to Travian (simulating iframe → parent)
      window.postMessage('videoEnds:' + vrid + ':' + sign, '*');
      log('📨 Posted videoEnds message to parent window');

      // Step 5: Also try calling the Travian API directly
      setTimeout(async () => {
        try {
          log('🎁 Calling /videofeature/ends directly...');
          const endsResp = await fetch('/api/v1/videofeature/ends', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ vrid: vrid, hash: sign })
          });
          const result = await endsResp.json();
          log('🎁 ends response:', JSON.stringify(result));

          if (result.error) {
            warn('⚠️ Server rejected:', result.error);
          } else {
            log('🏆 REWARD CLAIMED! Reloading...');
            setTimeout(() => location.reload(), 1000);
          }
        } catch (e) {
          warn('ends call error:', e);
        }
      }, 500);

    } catch (e) {
      warn('❌ Simulation failed:', e);
    }
  }

  // ── Also provide a manual trigger function ──
  window.instantReward = async function (type = 'productionBoost') {
    log('🎯 Manual trigger for:', type);

    // Step 1: Call open
    const openResp = await fetch('/api/v1/videofeature/open/' + type, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({})
    });
    const openData = await openResp.json();
    log('📦 Open response:', JSON.stringify(openData));

    if (openData.vrid && openData.videoIframeUrl) {
      await simulateVideoPlayback(openData.vrid, openData.videoIframeUrl);
    } else {
      warn('❌ Open did not return vrid/iframeUrl:', openData);
    }
  };

  log('');
  log('🎮 MANUAL USAGE: Run in console:');
  log('  instantReward("productionBoost")');
  log('  instantReward("buildingUpgrade")');
  log('  instantReward("adventureDuration")');
  log('  instantReward("smithyUpgrade")');
  log('  instantReward("academyResearch")');
  log('  instantReward("lumberProductionBonus")');
  log('  instantReward("clayProductionBonus")');
  log('  instantReward("ironProductionBonus")');
  log('  instantReward("cropProductionBonus")');
  log('');
  log('Or just click any "Watch video" button — it auto-intercepts!');

})();
