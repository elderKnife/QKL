package prototype

import (
	"net/url"
	"strings"
	"testing"
)

func TestGridFiltersBuildScopedPredicates(t *testing.T) {
	q:=url.Values{"packetId":{"packet-1"},"vppId":{"VPP-001"},"edgeId":{"EDGE-002"},"deviceId":{"PV-02"},"from":{"2026-11-04T10:00:00Z"},"to":{"2026-11-04T11:00:00Z"}}
	clauses,args,err:=gridFilters(q,"r");if err!=nil{t.Fatal(err)}
	if len(clauses)!=6||len(args)!=6{t.Fatalf("clauses=%v args=%v",clauses,args)}
	joined:=strings.Join(clauses," ")
	for _,field:=range []string{"r.packet_id","r.vpp_id","r.edge_id","r.device_id","r.collected_at"}{if !strings.Contains(joined,field){t.Fatalf("missing %s from %s",field,joined)}}
	if _,_,err=gridFilters(url.Values{"from":{"invalid"}},"g");err==nil{t.Fatal("invalid time filter must be rejected")}
}

func TestGridRecordFiltersUseJoinedBatchColumns(t *testing.T) {
	q := url.Values{"packetId": {"packet-1"}, "vppId": {"VPP-001"}, "edgeId": {"EDGE-002"}, "deviceId": {"PV-02"}}
	clauses, args, err := gridRecordFilters(q)
	if err != nil {
		t.Fatal(err)
	}
	joined := strings.Join(clauses, " ")
	for _, field := range []string{"r.packet_id", "b.vpp_id", "b.edge_id", "r.device_id"} {
		if !strings.Contains(joined, field) {
			t.Fatalf("missing %s from %s", field, joined)
		}
	}
	if len(clauses) != 4 || len(args) != 4 {
		t.Fatalf("clauses=%v args=%v", clauses, args)
	}
}
