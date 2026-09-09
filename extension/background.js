// background.js - Service Worker for API Requests

importScripts("stores.js");

// Resolve the sticky target store, then post there. Collection happens in
// category batches, so the operator sets this once per batch rather than the
// server guessing the category from the title.
function resolveTarget() {
    return new Promise((resolve) => {
        chrome.storage.local.get([DAJIAN_STORE_KEY], (result) => {
            resolve(dajianStoreById(result[DAJIAN_STORE_KEY] || DAJIAN_DEFAULT_STORE));
        });
    });
}

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
    if (request.action === "getTargetStore") {
        resolveTarget().then((store) => sendResponse({ store }));
        return true;
    }

    if (request.action === "collectProduct") {
        resolveTarget().then((store) => {
            const endpoint = `http://localhost:${store.port}/api/collect`;
            console.log(`Background: Sending ${request.data.sku} -> ${store.name} (${endpoint})`);

            fetch(endpoint, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(request.data)
            })
                .then(async response => {
                    const text = await response.text();
                    const contentType = response.headers.get("content-type") || "";

                    let data = null;
                    if (contentType.includes("application/json")) {
                        try {
                            data = JSON.parse(text);
                        } catch (error) {
                            console.error("Background: Invalid JSON response:", text.substring(0, 300));
                            throw new Error(`Server returned invalid JSON: ${text.substring(0, 120)}`);
                        }
                    }

                    if (!response.ok) {
                        const detail = (data && (data.message || data.error)) || text.substring(0, 200);
                        console.error(`Background: HTTP Error ${response.status}`, detail);
                        throw new Error(`HTTP ${response.status}: ${detail}`);
                    }

                    if (data) {
                        return data;
                    }

                    console.error("Background: Non-JSON response:", text.substring(0, 200));
                    throw new Error(`Server returned non-JSON response: ${text.substring(0, 120)}`);
                })
                .then(data => {
                    console.log("Background: Success", data);
                    sendResponse({ status: "success", data: data, store: store });
                })
                .catch(error => {
                    console.error("Background: Error", error);
                    // Name the target: a connection failure usually means that
                    // store's server simply isn't running on its port.
                    sendResponse({
                        status: "error",
                        message: `[${store.short}:${store.port}] ${error.toString()}`,
                        store: store
                    });
                });
        });

        return true; // Keep the message channel open for async response
    }
});
