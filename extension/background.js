// background.js - Service Worker for API Requests

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
    if (request.action === "collectProduct") {

        console.log("Background: Sending data to server...", request.data.sku);

        fetch("http://localhost:8000/api/collect", {
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
                sendResponse({ status: "success", data: data });
            })
            .catch(error => {
                console.error("Background: Error", error);
                sendResponse({ status: "error", message: error.toString() });
            });

        return true; // Keep the message channel open for async response
    }
});
