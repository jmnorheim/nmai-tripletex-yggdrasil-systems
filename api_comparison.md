# API Comparison: openapi.json vs api_reference.md

**Comparison Date:** 2026-03-22  
**OpenAPI Version:** 2.74.00  
**Total Endpoints in openapi.json:** 546  
**Endpoints in api_reference.md:** ~105

---

## Executive Summary

**api_reference.md is a curated subset** for common use cases (hackathon-focused), while **openapi.json contains the complete API**. All endpoints documented in api_reference.md are accurate, but many advanced operations are omitted.

---

## 1. Endpoints Missing in api_reference.md

### Employee Module
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/employee/employment` | GET | List employments |
| `/employee/employment/{id}` | GET/PUT | Employment management |
| `/employee/employment/occupationCode` | GET | Look up occupation codes |
| `/employee/category` | GET/POST | Employee categories |
| `/employee/nextOfKin` | GET/POST | Emergency contacts |
| `/employee/{id}/:upload` | POST | Upload employee photo |
| `/employee/list` | POST | Batch create employees |

### Customer Module
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/customer/category` | GET/POST | Customer categories |
| `/customer/{id}` | DELETE | Delete customer |
| `/customer/list` | POST | Batch create customers |

### Product Module
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/product/{id}` | DELETE | Delete product |
| `/product/group` | GET/POST | Product groups |
| `/product/group/{id}` | GET/PUT/DELETE | Product group management |
| `/product/inventory` | GET/POST | Inventory management |
| `/product/location` | GET/POST | Inventory locations |

### Order Module
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/order/{id}/:invoice` | PUT | Create invoice from order |
| `/order/{id}/:sendInvoicePreview` | GET | Preview invoice before sending |
| `/order/{id}/:send` | PUT | Send order confirmation |
| `/order/{id}/:copy` | POST | Copy order |
| `/order/orderline/{id}/:pick` | PUT | Mark line as picked |
| `/order/orderline/{id}/:unpick` | PUT | Unpick line |

### Invoice Module
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/invoice/details` | GET | Detailed invoice list |
| `/invoice/{id}/:createReminder` | PUT | Create payment reminder |
| `/invoice/{id}/pdf` | GET | Download invoice PDF |
| `/invoice/{id}/attachment` | POST | Add attachment |
| `/invoice/list` | POST | Batch create invoices |

### Travel Expense Module
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/travelExpense/:copy` | POST | Copy travel expense |
| `/travelExpense/:unapprove` | PUT | Unapprove expense |
| `/travelExpense/:reject` | PUT | Reject expense |
| `/travelExpense/{id}/attachment` | POST/GET | Attachments |
| `/travelExpense/cost/{id}` | GET/PUT/DELETE | Cost management |
| `/travelExpense/mileageAllowance/{id}` | GET/PUT/DELETE | Mileage management |
| `/travelExpense/perDiemCompensation/{id}` | GET/PUT/DELETE | Per diem management |

### Project Module
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/project/{id}/period` | GET/POST | Project periods |
| `/project/{id}/period/{periodId}` | GET/PUT/DELETE | Period management |
| `/project/orderline` | GET | Project order lines |
| `/project/category/{id}` | GET/PUT/DELETE | Category management |
| `/project/list` | POST | Batch create projects |

### Ledger & Vouchers Module
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/ledger/account/{id}` | GET/PUT/DELETE | Account management |
| `/ledger/account/list` | POST | Batch create accounts |
| `/ledger/voucher/{id}` | DELETE | Delete voucher |
| `/ledger/voucher/{id}/pdf` | GET | Voucher PDF |
| `/ledger/voucher/{id}/attachment` | POST | Add attachment |
| `/ledger/voucher/list` | POST | Batch create vouchers |
| `/ledger/posting/{id}` | GET | Single posting |
| `/ledger/voucherType/{id}` | GET/PUT/DELETE | Voucher type management |

### Timesheet Module
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/timesheet/week` | GET/POST | Weekly timesheets |
| `/timesheet/month` | GET | Monthly overview |
| `/timesheet/timeclock` | GET/POST | Time clock entries |
| `/timesheet/allocatedHours` | GET/POST | Allocated hours |
| `/timesheet/entry/list` | PUT | Batch update entries |

### Bank Module
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/bank/{id}` | GET | Single bank |
| `/bank/statement` | GET/POST | Bank statements |
| `/bank/statement/{id}` | GET/PUT/DELETE | Statement management |
| `/bank/reconciliation/{id}/:match` | PUT | Match transactions |
| `/bank/reconciliation/{id}/:unmatch` | PUT | Unmatch transactions |
| `/bank/reconciliation/{id}/:close` | PUT | Close reconciliation |

### Company Module
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/company/{id}` | PUT | Update company |
| `/company/{id}/:uploadLogo` | POST | Upload logo |
| `/company/salesmodules/{id}` | DELETE | Deactivate module |

---

## 2. Query Parameter Differences

### api_reference.md Documents Minimal Parameters
- Usually only entity-specific filters (e.g., `customerName`, `email`)

### openapi.json Includes Full Filtering
All list endpoints support:
- `from` - Pagination start index (default: 0)
- `count` - Number of results (default: 1000)
- `sorting` - Sort pattern
- `fields` - Field selection pattern

### Specific Parameter Gaps

#### Invoice Endpoint (`/invoice`)
**Documented in api_reference.md:**
- `invoiceDateFrom` (required)
- `invoiceDateTo`
- `customerId`
- `invoiceNumber`
- `kid`

**Missing from api_reference.md (exist in openapi.json):**
- `isPaid` - Filter by payment status
- `showPayments` - Include payment info
- `showHistoricPostings` - Include historic postings
- `invoiceDueDateFrom/To` - Due date range
- `amountFrom/To` - Amount range
- `ourRef` - Reference filter

#### Employee Endpoint (`/employee`)
**Missing from api_reference.md:**
- `departmentId` - Filter by department
- `divisionId` - Filter by division
- `isContact` - Include/exclude contacts
- `isUser` - Filter by user status

---

## 3. Path Parameter Verification

✅ **All path parameters match correctly between files**

Examples:
- `/employee/{id}` - id: integer, int64 format
- `/customer/{id}` - id: integer, int64 format
- `/order/{id}` - id: integer, int64 format
- `/invoice/{id}` - id: integer, int64 format
- `/project/{id}` - id: integer, int64 format

---

## 4. Request/Response Body Fields

### api_reference.md Approach
- Provides **example JSON** for common fields
- Focuses on required fields for basic operations
- Does not document all available fields

### openapi.json Approach
- Complete JSON Schema definitions
- All fields documented with types and formats
- Includes validation rules

### Example: Product Creation

**api_reference.md example:**
```json
{
  "name": "Konsulenttime",
  "number": "1001",
  "priceExcludingVatCurrency": 1200.00,
  "vatType": {"id": 3},
  "productUnit": {"id": 1},
  "account": {"id": 3000},
  "isStockItem": false
}
```

**Additional fields in openapi.json:**
- `description` - Product description
- `costExcludingVatCurrency` - Cost price
- `isInactive` - Active status
- `isNuclearMedicine` - Special flag
- `productGroup` - Group reference
- `stockQuantity` - Current stock
- `stockLimit` - Reorder limit
- `inventoryLocation` - Location reference

---

## 5. Correctly Documented Limitations

✅ **GET /company** - Correctly documented as "does NOT exist"
- Only PUT /company/{id} exists for updates
- No list endpoint available

---

## 6. HTTP Method Verification

✅ **All documented methods match openapi.json:**

| Endpoint | api_reference.md | openapi.json | Match |
|----------|-----------------|--------------|-------|
| GET /employee | ✅ | ✅ | ✅ |
| POST /employee | ✅ | ✅ | ✅ |
| GET /employee/{id} | ✅ | ✅ | ✅ |
| PUT /employee/{id} | ✅ | ✅ | ✅ |
| GET /customer | ✅ | ✅ | ✅ |
| POST /customer | ✅ | ✅ | ✅ |
| GET /customer/{id} | ✅ | ✅ | ✅ |
| PUT /customer/{id} | ✅ | ✅ | ✅ |
| GET /order | ✅ | ✅ | ✅ |
| POST /order | ✅ | ✅ | ✅ |
| GET /order/{id} | ✅ | ✅ | ✅ |
| PUT /order/{id} | ✅ | ✅ | ✅ |
| POST /order/orderline | ✅ | ✅ | ✅ |
| PUT /order/orderline/{id} | ✅ | ✅ | ✅ |
| DELETE /order/orderline/{id} | ✅ | ✅ | ✅ |
| GET /invoice | ✅ | ✅ | ✅ |
| POST /invoice | ✅ | ✅ | ✅ |
| GET /invoice/{id} | ✅ | ✅ | ✅ |
| PUT /invoice/{id}/:payment | ✅ | ✅ | ✅ |
| PUT /invoice/{id}/:createCreditNote | ✅ | ✅ | ✅ |
| PUT /invoice/{id}/:send | ✅ | ✅ | ✅ |

---

## 7. Recommendations

### For api_reference.md Updates

**Priority 1 - Critical Missing Endpoints:**
1. Add `/order/{id}/:invoice` - Essential for order-to-invoice workflow
2. Add `/invoice/{id}/pdf` - PDF download commonly needed
3. Add `invoice.isPaid` parameter - Critical for filtering
4. Add batch endpoints (`/list`) for bulk operations

**Priority 2 - Useful Additions:**
1. Add DELETE endpoints for resource cleanup
2. Add attachment endpoints for document handling
3. Add project period endpoints
4. Add timesheet week/month endpoints

**Priority 3 - Complete Coverage:**
1. Document all query parameters (from, count, sorting, fields)
2. Add sub-resource endpoints (costs, mileage, per diem)
3. Add bank statement endpoints
4. Add ledger DELETE endpoints

### For Implementation Guidance

**When using api_reference.md:**
- ✅ Use for 80% of common use cases
- ✅ All documented endpoints are accurate
- ⚠️ Check openapi.json for advanced features
- ⚠️ Add pagination params (from, count) for large datasets
- ⚠️ Use fields parameter to optimize responses

**When openapi.json is needed:**
- Batch operations (/list endpoints)
- PDF generation
- Attachment handling
- Advanced filtering
- Resource deletion
- Sub-resource management

---

## 8. Endpoint Count Comparison

| Module | api_reference.md | openapi.json | Coverage |
|--------|------------------|--------------|----------|
| Employee | 4 | 25+ | 16% |
| Customer | 4 | 15+ | 27% |
| Contact | 4 | 10+ | 40% |
| Product | 5 | 25+ | 20% |
| Order | 7 | 35+ | 20% |
| Invoice | 6 | 30+ | 20% |
| Travel Expense | 12 | 45+ | 27% |
| Project | 5 | 30+ | 17% |
| Department | 4 | 8+ | 50% |
| Ledger/Vouchers | 10 | 60+ | 17% |
| Supplier | 4 | 15+ | 27% |
| Bank | 4 | 25+ | 16% |
| Company | 3 | 10+ | 30% |
| Timesheet | 10 | 35+ | 29% |
| **Total** | **~105** | **546** | **19%** |

---

## Conclusion

**api_reference.md serves its purpose as a hackathon-focused, curated guide** covering the most common 19% of endpoints. All documented information is accurate.

**For production use or advanced features, refer to openapi.json directly** or expand api_reference.md with Priority 1 and 2 recommendations above.
