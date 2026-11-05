package prototype

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestWebRequiresUpstreamsTokenAndStaticAssets(t *testing.T) {
	if _, err := NewWebHandler(WebConfig{EdgeURL: "http://edge:8080", VPPURL: "http://vpp:8081", Token: "secret", AssetDir: "missing"}); err != nil {
		t.Fatalf("handler should initialize while asset check stays request-time: %v", err)
	}
	if _, err := NewWebHandler(WebConfig{EdgeURL: "http://edge:8080", VPPURL: "http://vpp:8081", AssetDir: "missing"}); err == nil {
		t.Fatal("missing service token must fail closed")
	}
	if _, err := NewWebHandler(WebConfig{EdgeURL: "http://user:secret@example.test", VPPURL: "http://vpp:1", Token: "x", AssetDir: "missing"}); err == nil {
		t.Fatal("upstream credentials must not be accepted")
	}
}

func TestWebLocalHostProtectionRejectsRemoteAndCrossSiteWrites(t *testing.T) {
	next := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(204) })
	handler := localWebGuard(next)
	for _, test := range []struct {
		name, host, origin, fetch, marker string
		want                              int
	}{
		{"remote host", "example.com", "", "", "", 403},
		{"cross-site origin", "127.0.0.1:18082", "http://attacker.example", "", "1", 403},
		{"missing marker", "127.0.0.1:18082", "", "same-origin", "", 403},
		{"bad content type", "127.0.0.1:18082", "http://127.0.0.1:18082", "same-origin", "1", 415},
	} {
		t.Run(test.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodPost, "http://"+test.host+"/api/edge/simulation/pause", strings.NewReader("{}"))
			req.Host = test.host
			req.Header.Set("Origin", test.origin)
			req.Header.Set("Sec-Fetch-Site", test.fetch)
			req.Header.Set("X-Prototype-Request", test.marker)
			if test.fetch == "same-origin" {
				req.Header.Set("Content-Type", "text/plain")
			}
			rec := httptest.NewRecorder()
			handler.ServeHTTP(rec, req)
			if rec.Code != test.want {
				t.Fatalf("got %d want %d: %s", rec.Code, test.want, rec.Body)
			}
		})
	}
}

func TestWebOverviewReturnsDependencyErrorsWithoutFakeSuccess(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/ui/overview" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		if r.Header.Get("Authorization") != "Bearer secret" {
			t.Error("proxy did not supply internal credential")
		}
		if r.Host == "127.0.0.1:18082" {
			t.Error("internal request retained browser host")
		}
		writeTestJSON(w, map[string]any{"database": "ok", "records": 3})
	}))
	defer upstream.Close()
	url := strings.TrimPrefix(upstream.URL, "http://")
	handler, err := NewWebHandler(WebConfig{EdgeURL: "http://" + url, VPPURL: "http://127.0.0.1:1", Token: "secret", AssetDir: "missing"})
	if err != nil {
		t.Fatal(err)
	}
	login := httptest.NewRequest(http.MethodPost,"http://127.0.0.1:18082/api/auth/login",strings.NewReader(`{"username":"vpp","password":"vpp-local-password"}`))
	login.Header.Set("Content-Type","application/json");login.Header.Set("X-Prototype-Request","1")
	loginRec:=httptest.NewRecorder();handler.ServeHTTP(loginRec,login)
	if loginRec.Code!=200 { t.Fatalf("login failed: %d %s",loginRec.Code,loginRec.Body) }
	var cookie *http.Cookie;for _,candidate:=range loginRec.Result().Cookies(){if candidate.Name=="prototype_session"{cookie=candidate}}
	if cookie==nil{t.Fatal("login did not create session cookie")}
	req := httptest.NewRequest(http.MethodGet, "http://127.0.0.1:18082/api/overview", nil)
	req.AddCookie(cookie)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != 200 {
		t.Fatalf("got HTTP %d: %s", rec.Code, rec.Body)
	}
	var result map[string]any
	if err = json.Unmarshal(rec.Body.Bytes(), &result); err != nil {
		t.Fatal(err)
	}
	edge := result["edge"].(map[string]any)
	if edge["available"] != true {
		t.Fatalf("healthy dependency unavailable: %+v", edge)
	}
	vpp := result["vpp"].(map[string]any)
	if vpp["available"] != false || !strings.Contains(vpp["error"].(string), "不可用") {
		t.Fatalf("unavailable dependency was presented as success: %+v", vpp)
	}
}

func TestManagementWritesRequireInternalTokenAndEmptyJSON(t *testing.T) {
	next := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { writeTestJSON(w, map[string]any{"ok": true}) })
	handler := controlAuth("secret", next)
	for _, test := range []struct {
		name, authorization, body string
		want                      int
	}{{"missing credential", "", "{}", 401}, {"wrong credential", "Bearer wrong", "{}", 401}, {"nonempty request", "Bearer secret", `{"running":true}`, 400}, {"empty object", "Bearer secret", "{}", 200}} {
		t.Run(test.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodPost, "http://edge/v1/ui/simulation/pause", strings.NewReader(test.body))
			req.Header.Set("Content-Type", "application/json")
			req.Header.Set("Authorization", test.authorization)
			rec := httptest.NewRecorder()
			handler.ServeHTTP(rec, req)
			if rec.Code != test.want {
				t.Fatalf("got %d want %d: %s", rec.Code, test.want, rec.Body)
			}
		})
	}
}

func writeTestJSON(w http.ResponseWriter, value any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(value)
}
