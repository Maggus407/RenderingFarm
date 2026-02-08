from backend.app import app


if __name__ == "__main__":
    from backend import context

    app.run(
        host=str(context.CONFIG.get("HOST", "0.0.0.0")),
        port=int(context.CONFIG.get("PORT", 5000)),
        debug=False,
    )
