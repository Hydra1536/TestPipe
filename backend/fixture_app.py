import secrets
from fastapi import FastAPI, Request, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse

app = FastAPI()
SESSIONS = {}

def page(title, body):
    return f"""<!doctype html><html><head><title>{title}</title></head><body>
    <nav><a href="/">Home</a> | <a href="/about">About</a> | <a href="/products">Products</a>
    | <a href="/login">Login</a> | <a href="/signup">Sign up</a> | <a href="/dashboard">Dashboard</a></nav>
    <h1>{title}</h1>{body}</body></html>"""

def user(req: Request):
    return SESSIONS.get(req.cookies.get("sid"))

@app.get("/", response_class=HTMLResponse)
def home():
    return page("Demo Home", "<p>Welcome to the demo shop.</p><a href='/products'>See products</a>")

@app.get("/about", response_class=HTMLResponse)
def about():
    return page("About Us", "<p>We sell demo widgets since 2020.</p>")

@app.get("/products", response_class=HTMLResponse)
def products():
    return page("Products", "<ul><li>Widget A</li><li>Widget B</li></ul><a href='/products/1'>Widget A details</a>")

@app.get("/products/1", response_class=HTMLResponse)
def product1():
    return page("Widget A", "<p>A great widget.</p><button>Add to cart</button>")

@app.get("/signup", response_class=HTMLResponse)
def signup_form():
    return page("Sign Up", """<form method="post" action="/signup">
    <input name="email" type="email" placeholder="Email" required>
    <input name="password" type="password" placeholder="Password" required>
    <input name="confirm" type="password" placeholder="Confirm password" required>
    <button type="submit">Create account</button></form>""")

@app.post("/signup")
def signup(email: str = Form(...), password: str = Form(...), confirm: str = Form("")):
    sid = secrets.token_hex(8); SESSIONS[sid] = email
    r = RedirectResponse("/dashboard", status_code=303); r.set_cookie("sid", sid); return r

@app.get("/login", response_class=HTMLResponse)
def login_form():
    return page("Login", """<form method="post" action="/login">
    <input name="email" type="email" placeholder="Email" required>
    <input name="password" type="password" placeholder="Password" required>
    <button type="submit">Sign in</button></form>""")

@app.post("/login")
def login(email: str = Form(...), password: str = Form(...)):
    sid = secrets.token_hex(8); SESSIONS[sid] = email
    r = RedirectResponse("/dashboard", status_code=303); r.set_cookie("sid", sid); return r

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(req: Request):
    u = user(req)
    if not u:
        return RedirectResponse("/login", status_code=303)
    return page("Dashboard", f"<p>Signed in as {u}</p><a href='/logout'>Logout</a>"
                "<a href='/settings'>Settings</a>")

@app.get("/settings", response_class=HTMLResponse)
def settings(req: Request):
    if not user(req):
        return RedirectResponse("/login", status_code=303)
    return page("Settings", "<form><input name='bio' placeholder='Bio'><button>Save</button></form><a href='/logout'>Logout</a>")

@app.get("/logout")
def logout():
    r = RedirectResponse("/", status_code=303); r.delete_cookie("sid"); return r
