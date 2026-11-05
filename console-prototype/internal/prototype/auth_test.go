package prototype

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestLocalRoleLoginAndAuthorization(t *testing.T){
	store:=newSessionStore(WebConfig{VPPUsername:"vpp",VPPPassword:"vpp-secret",GRIDUsername:"grid",GRIDPassword:"grid-secret"})
	mux:=http.NewServeMux();store.routes(mux)
	mux.Handle("GET /api/vpp/private",requireRole(store,[]string{"vpp"},http.HandlerFunc(func(w http.ResponseWriter,r *http.Request){w.WriteHeader(204)})))
	mux.Handle("GET /api/grid/private",requireRole(store,[]string{"grid"},http.HandlerFunc(func(w http.ResponseWriter,r *http.Request){w.WriteHeader(204)})))
	login:=func(username,password string)(int,*http.Cookie){body,_:=json.Marshal(map[string]string{"username":username,"password":password});req:=httptest.NewRequest(http.MethodPost,"/api/auth/login",strings.NewReader(string(body)));rec:=httptest.NewRecorder();mux.ServeHTTP(rec,req);for _,cookie:=range rec.Result().Cookies(){if cookie.Name=="prototype_session"{return rec.Code,cookie}};return rec.Code,nil}
	if status,cookie:=login("vpp","wrong");status!=401||cookie!=nil{t.Fatalf("invalid login status=%d cookie=%v",status,cookie)}
	status,cookie:=login("vpp","vpp-secret");if status!=200||cookie==nil{t.Fatalf("valid login status=%d cookie=%v",status,cookie)}
	for _,test:=range []struct{path string;want int}{{"/api/vpp/private",204},{"/api/grid/private",403}}{req:=httptest.NewRequest(http.MethodGet,test.path,nil);req.AddCookie(cookie);rec:=httptest.NewRecorder();mux.ServeHTTP(rec,req);if rec.Code!=test.want{t.Fatalf("%s status=%d want=%d",test.path,rec.Code,test.want)}}
	logout:=httptest.NewRequest(http.MethodPost,"/api/auth/logout",nil);logout.AddCookie(cookie);mux.ServeHTTP(httptest.NewRecorder(),logout)
	req:=httptest.NewRequest(http.MethodGet,"/api/vpp/private",nil);req.AddCookie(cookie);rec:=httptest.NewRecorder();mux.ServeHTTP(rec,req);if rec.Code!=401{t.Fatalf("logged-out session status=%d",rec.Code)}
}
