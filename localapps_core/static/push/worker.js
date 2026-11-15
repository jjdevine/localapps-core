// Push notifications in the service worker: showing a message when one arrives, and opening
// the app when it is tapped. Served at /static/push/worker.js by localapps_core.web.push.
//
// An app's own service worker loads it and says what its notifications look like:
//
//   importScripts("/static/push/worker.js");
//   localappsPush({ title: "Notes", tag: "notes", icon: "/static/icon-192.png", badge: "/static/badge-96.png" });
//
// title, tag and url (default "/") are what a message is shown with when it does not carry its
// own. icon is the colour picture in the expanded notification. badge is the status-bar glyph,
// which Android draws as a silhouette from the image's transparency, so it must be a shape on
// a transparent background, not the square app icon. navigate: false leaves an open copy of
// the app where it is when a notification is tapped (a single-page app would lose its place);
// otherwise the open copy is taken to the message's url.
//
// The message is the JSON the server sends: {title, body, url, tag}. Call localappsPush while
// the worker's script is first run, not later: a browser only honours push handlers added then.
"use strict";

self.localappsPush = function (options) {
  options = options || {};

  self.addEventListener("push", function (event) {
    var message = { title: options.title || "", body: "", url: options.url || "/", tag: options.tag || "" };
    if (event.data) {
      try { Object.assign(message, event.data.json()); } catch (e) { message.body = event.data.text(); }
    }
    event.waitUntil(self.registration.showNotification(message.title, {
      body: message.body,
      tag: message.tag,
      renotify: !!message.tag,
      icon: options.icon,
      badge: options.badge,
      data: { url: message.url }
    }));
  });

  // Tapping the notification brings an open copy of the app to the front, or opens one.
  self.addEventListener("notificationclick", function (event) {
    event.notification.close();
    var target = (event.notification.data && event.notification.data.url) || options.url || "/";
    var url = new URL(target, self.location.origin).href;
    event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (clients) {
      for (var i = 0; i < clients.length; i++) {
        if (clients[i].url.indexOf(self.location.origin) === 0 && "focus" in clients[i]) {
          if (options.navigate !== false && "navigate" in clients[i]) clients[i].navigate(url);
          return clients[i].focus();
        }
      }
      return self.clients.openWindow(url);
    }));
  });
};
