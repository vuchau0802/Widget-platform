(function() {
    var scripts = document.getElementsByTagName("script");
    var thisScript = scripts[scripts.length - 1];
    var srcUrl = new URL(thisScript.src);
    var widgetId = srcUrl.searchParams.get("id");
    var apiOrigin = srcUrl.origin;

    if (!widgetId) {
        console.error("Widget script: no widget id found in script src");
        return;
    }

    fetch(apiOrigin + "/widgets/" + widgetId + "/config")
        .then(function(res) {
            return res.json();
        })
        .then(function(config) {
            renderWidget(config);
        })
        .catch(function(err) {
            console.error("Widget failed to load config:", err);
        });

    function renderWidget(config) {
        var container = document.createElement("div");
        container.style.cssText =
            "border:1px solid #ccc; border-radius:8px; padding:16px; max-width:320px; font-family:sans-serif;";

        var title = document.createElement("h3");
        title.textContent = config.title;
        container.appendChild(title);

        var form = document.createElement("form");

        (config.fields || []).forEach(function(field) {
            var label = document.createElement("label");
            label.textContent = field.label;
            label.style.display = "block";
            label.style.marginTop = "8px";

            var input = document.createElement("input");
            input.type = field.type || "text";
            input.name = field.name;
            input.required = !!field.required;
            input.style.cssText = "width:100%; padding:6px; margin-top:4px; box-sizing:border-box;";

            label.appendChild(input);
            form.appendChild(label);
        });

        var honeypot = document.createElement("input");
        honeypot.type = "text";
        honeypot.name = "website";
        honeypot.tabIndex = -1;
        honeypot.autocomplete = "off";
        honeypot.style.cssText =
            "position:absolute; left:-9999px; width:1px; height:1px; overflow:hidden;";
        form.appendChild(honeypot);

        var submitBtn = document.createElement("button");
        submitBtn.type = "submit";
        submitBtn.textContent = config.button_text || "Submit";
        submitBtn.style.cssText = "margin-top:12px; padding:8px 16px; cursor:pointer;";
        form.appendChild(submitBtn);

        var statusMsg = document.createElement("p");
        statusMsg.style.cssText = "margin-top:8px; font-size:13px;";
        form.appendChild(statusMsg);

        form.addEventListener("submit", function(e) {
            e.preventDefault();
            var formData = new FormData(form);
            var data = {};
            formData.forEach(function(value, key) {
                if (key !== "website") data[key] = value;
            });
            var honeypotValue = formData.get("website") || "";

            fetch(apiOrigin + "/submissions", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ widget_id: parseInt(widgetId, 10), data: data, website: honeypotValue }),
                })
                .then(function(res) {
                    if (res.status === 429) {
                        statusMsg.textContent = "Too many requests — please wait a moment and try again.";
                        statusMsg.style.color = "red";
                        return;
                    }
                    if (!res.ok) {
                        statusMsg.textContent = "Something went wrong. Please check your input.";
                        statusMsg.style.color = "red";
                        return;
                    }
                    statusMsg.textContent = "Thanks! Your submission was received.";
                    statusMsg.style.color = "green";
                    form.reset();
                })
                .catch(function() {
                    statusMsg.textContent = "Network error. Please try again.";
                    statusMsg.style.color = "red";
                });
        });

        container.appendChild(form);
        thisScript.parentNode.insertBefore(container, thisScript.nextSibling);
    }
})();