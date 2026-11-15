// Push notifications in the page: whether this browser is subscribed, and turning it on or off.
// Served at /static/push/page.js by localapps_core.web.push.
//
// It does two things. It defines window.LocalappsPush, the browser's side of subscribing, for a
// page that draws its own controls. And, on a page with a Notifications section marked up as
// below, it runs that section, talking to the routes in push_router:
//
//   <section id="push" data-public-key="..." data-endpoints='["https://...", ...]'>
//     <p data-push-status>Notifications need JavaScript, which is off or still loading.</p>
//     <button type="button" data-push-toggle hidden>Turn on on this device</button>
//   </section>
//
// data-public-key is the app's VAPID public key and data-endpoints the endpoints the server
// holds. After a change the page is reloaded with ?device=added or ?device=removed.
(function (root) {
  "use strict";

  var READY_TIMEOUT_MS = 5000;

  // Why this browser cannot be turned on, in words for the screen; "" when it can.
  function problem() {
    if (!("serviceWorker" in navigator) || !("PushManager" in root) || !("Notification" in root)) {
      return "This browser cannot receive push notifications. On Android use Chrome, ideally with the app added to the home screen.";
    }
    if (!root.isSecureContext) {
      return "Notifications only work over HTTPS. Open the app at its HTTPS address to turn them on.";
    }
    if (Notification.permission === "denied") {
      return "Notifications are blocked for this site. Allow them in the browser's site settings, then come back here.";
    }
    return "";
  }

  // The VAPID public key as the bytes PushManager.subscribe wants.
  function keyBytes(base64url) {
    var padded = base64url + "=".repeat((4 - (base64url.length % 4)) % 4);
    var raw = atob(padded.replace(/-/g, "+").replace(/_/g, "/"));
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  // The page's service worker registration, once it is active. /sw.js is registered here only
  // if the page has not registered a service worker of its own.
  function registration() {
    var timeout = new Promise(function (_resolve, reject) {
      setTimeout(function () {
        reject(new Error("the service worker is not ready; reload the page and try again"));
      }, READY_TIMEOUT_MS);
    });
    var ready = navigator.serviceWorker.getRegistration().then(function (existing) {
      return existing || navigator.serviceWorker.register("/sw.js");
    }).then(function () { return navigator.serviceWorker.ready; });
    return Promise.race([ready, timeout]);
  }

  // This browser's PushSubscription, or null when it holds none or cannot hold one.
  function subscription() {
    if (problem()) return Promise.resolve(null);
    return registration().then(function (reg) { return reg.pushManager.getSubscription(); });
  }

  // Ask for permission and subscribe under the app's key. A subscription the browser already
  // holds is dropped and made afresh: the server may have forgotten it, or it may have been
  // made under an older key. Resolves to the new PushSubscription, for the page to post.
  function subscribe(publicKey) {
    return Notification.requestPermission().then(function (permission) {
      if (permission !== "granted") throw new Error("permission was not given");
      return registration();
    }).then(function (reg) {
      return reg.pushManager.getSubscription().then(function (stale) {
        return stale ? stale.unsubscribe() : null;
      }).then(function () {
        return reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(publicKey) });
      });
    });
  }

  root.LocalappsPush = {
    problem: problem,
    keyBytes: keyBytes,
    registration: registration,
    subscription: subscription,
    subscribe: subscribe
  };

  // --- the Notifications section of a server-rendered Settings page ---------------------------

  var section = document.getElementById("push");
  if (!section) return;
  var status = section.querySelector("[data-push-status]");
  var button = section.querySelector("[data-push-toggle]");
  if (!status || !button) return;
  var publicKey = section.getAttribute("data-public-key");
  var known = JSON.parse(section.getAttribute("data-endpoints") || "[]");
  var current = null;

  function say(text) { status.textContent = text; }

  function reason(err) { return err && err.message ? err.message : err; }

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (response) {
      if (response.ok) return null;
      return response.json().catch(function () { return {}; }).then(function (answer) {
        throw new Error(typeof answer.detail === "string" ? answer.detail : "The server answered " + response.status);
      });
    });
  }

  function reload(what) { location.href = location.pathname + "?device=" + what; }

  // On means the browser holds a subscription and the server still lists it. A device removed
  // from the list elsewhere, or subscribed under an older key, shows as off.
  function show(sub) {
    current = sub;
    var on = !!sub && known.indexOf(sub.endpoint) >= 0;
    say(on ? "This device gets notifications." : "This device does not get notifications yet.");
    button.textContent = on ? "Turn off on this device" : "Turn on on this device";
    button.setAttribute("data-on", on ? "1" : "");
    button.hidden = false;
    button.disabled = false;
  }

  function turnOn() {
    return subscribe(publicKey).then(function (sub) {
      return post("/notifications/subscribe", sub.toJSON());
    }).then(function () { reload("added"); });
  }

  function turnOff() {
    var sub = current;
    return post("/notifications/unsubscribe", { endpoint: sub.endpoint })
      .then(function () { return sub.unsubscribe(); })
      .then(function () { reload("removed"); });
  }

  function refresh() {
    var trouble = problem();
    if (trouble) {
      say(trouble);
      button.hidden = true;
      return Promise.resolve();
    }
    return subscription().then(show);
  }

  button.addEventListener("click", function () {
    button.disabled = true;
    say("One moment…");
    (button.getAttribute("data-on") ? turnOff() : turnOn()).catch(function (err) {
      var failure = "Could not change notifications: " + reason(err);
      return refresh().catch(function () {}).then(function () {
        if (!problem()) say(failure);
      });
    });
  });

  refresh().catch(function (err) {
    say("Could not set up notifications: " + reason(err));
  });
})(window);
