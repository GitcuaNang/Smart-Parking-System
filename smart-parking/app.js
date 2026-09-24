import { initializeApp }
    from "https://www.gstatic.com/firebasejs/12.19.0/firebase-app.js";

import {
    getDatabase,
    ref,
    onValue,
    update,
    push
}
from "https://www.gstatic.com/firebasejs/12.19.0/firebase-database.js";


/* =====================================
   FIREBASE CONFIG
===================================== */

const firebaseConfig = {

    apiKey: "DÁN_API_KEY_CỦA_BẠN",

    authDomain:
        "smart-parking-78632.firebaseapp.com",

    databaseURL:
        "https://smart-parking-78632-default-rtdb.firebaseio.com",

    projectId:
        "smart-parking-78632",

    storageBucket:
        "DÁN_STORAGE_BUCKET",

    messagingSenderId:
        "DÁN_MESSAGING_SENDER_ID",

    appId:
        "DÁN_APP_ID"
};


/* =====================================
   KHỞI TẠO FIREBASE
===================================== */

const app = initializeApp(firebaseConfig);

const db = getDatabase(app);


/* =====================================
   CẤU HÌNH
===================================== */

const slots = [
    "A1",
    "A2",
    "A3",
    "A4"
];


/* =====================================
   BIẾN DỮ LIỆU
===================================== */

let currentSlots = {};

let historyData = {};

let allHistory = [];


/* =====================================
   HÀM HIỂN THỊ THỜI GIAN
===================================== */

function formatTime(time) {

    if (!time) {
        return "--";
    }

    return time;
}


/* =====================================
   FIREBASE → SLOTS
===================================== */

const slotsRef = ref(db, "slots");


onValue(slotsRef, (snapshot) => {

    const data = snapshot.val() || {};

    currentSlots = data;

    updateParkingDashboard(data);

    updateVehiclesTable(data);

    updateHistoryStatistics();

});


/* =====================================
   CẬP NHẬT TRANG TỔNG QUAN
===================================== */

function updateParkingDashboard(data) {

    let occupiedCount = 0;


    slots.forEach(slot => {

        const slotData =
            data[slot] || {};


        const slotElement =
            document.getElementById(
                `slot-${slot}`
            );


        if (!slotElement) {
            return;
        }


        const plateElement =
            document.getElementById(
                `plate-${slot}`
            );


        const statusElement =
            slotElement.querySelector(
                ".status"
            );


        const carElement =
            slotElement.querySelector(
                ".car"
            );


        /* =========================
           CÓ XE
        ========================= */

        if (
            slotData.occupied === true
        ) {

            occupiedCount++;


            slotElement.classList.remove(
                "available"
            );

            slotElement.classList.add(
                "occupied"
            );


            statusElement.innerText =
                "🔴 Đang đỗ";


            carElement.innerText =
                "🚗";


            plateElement.innerText =
                slotData.plate || "---";

        }


        /* =========================
           TRỐNG
        ========================= */

        else {

            slotElement.classList.remove(
                "occupied"
            );

            slotElement.classList.add(
                "available"
            );


            statusElement.innerText =
                "🟢 Trống";


            carElement.innerText =
                "🅿️";


            plateElement.innerText =
                "---";

        }

    });


    const totalSlots =
        slots.length;


    const availableCount =
        totalSlots - occupiedCount;


    const totalElement =
        document.getElementById(
            "total"
        );


    const occupiedElement =
        document.getElementById(
            "occupied"
        );


    const availableElement =
        document.getElementById(
            "available"
        );


    if (totalElement) {

        totalElement.innerText =
            totalSlots;

    }


    if (occupiedElement) {

        occupiedElement.innerText =
            occupiedCount;

    }


    if (availableElement) {

        availableElement.innerText =
            availableCount;

    }


    const historyOccupied =
        document.getElementById(
            "historyOccupied"
        );


    if (historyOccupied) {

        historyOccupied.innerText =
            occupiedCount;

    }

}


/* =====================================
   TRANG XE TRONG BÃI
===================================== */

function updateVehiclesTable(data) {

    const table =
        document.getElementById(
            "vehiclesTable"
        );


    if (!table) {
        return;
    }


    let html = "";

    let count = 0;


    slots.forEach(slot => {

        const slotData =
            data[slot];


        if (
            slotData &&
            slotData.occupied === true
        ) {

            count++;


            let timeIn = "--";


            /*
             * Nếu slot có historyId,
             * tìm timeIn trong history.
             */

            if (
                slotData.historyId &&
                historyData[
                    slotData.historyId
                ]
            ) {

                timeIn =
                    historyData[
                        slotData.historyId
                    ].timeIn || "--";

            }


            html += `

                <tr>

                    <td>
                        <strong>
                            ${escapeHtml(
                                slotData.plate || "---"
                            )}
                        </strong>
                    </td>

                    <td>
                        ${slot}
                    </td>

                    <td>
                        ${escapeHtml(timeIn)}
                    </td>

                    <td>
                        <span class="status">
                            🟢 Đang đỗ
                        </span>
                    </td>

                </tr>

            `;

        }

    });


    if (count === 0) {

        html = `

            <tr>

                <td
                    colspan="4"
                    class="empty-message"
                >
                    Chưa có xe trong bãi
                </td>

            </tr>

        `;

    }


    table.innerHTML = html;


    const vehicleCount =
        document.getElementById(
            "vehicleCount"
        );


    if (vehicleCount) {

        vehicleCount.innerText =
            `${count} xe`;

    }

}


/* =====================================
   FIREBASE → HISTORY
===================================== */

const historyRef =
    ref(db, "history");


onValue(historyRef, (snapshot) => {

    historyData =
        snapshot.val() || {};


    allHistory =
        Object.entries(
            historyData
        ).map(([id, data]) => {

            return {
                id: id,
                ...data
            };

        });


    /*
     * Mới nhất lên đầu
     */

    allHistory.sort(
        (a, b) => {

            return (
                String(
                    b.timeIn || ""
                ).localeCompare(
                    String(
                        a.timeIn || ""
                    )
                )
            );

        }
    );


    updateHistoryTable();

    updateHistoryStatistics();

    /*
     * Khi history thay đổi,
     * cập nhật lại bảng xe đang đỗ
     */

    updateVehiclesTable(
        currentSlots
    );

});


/* =====================================
   HIỂN THỊ HISTORY
===================================== */

function updateHistoryTable(
    searchText = ""
) {

    const table =
        document.getElementById(
            "historyTable"
        );


    if (!table) {
        return;
    }


    const keyword =
        searchText
            .trim()
            .toLowerCase();


    const filtered =
        allHistory.filter(item => {

            if (!keyword) {
                return true;
            }


            return String(
                item.plate || ""
            )
                .toLowerCase()
                .includes(keyword);

        });


    let html = "";


    filtered.forEach(item => {

        const imageIn =
            item.imageIn || "";


        const imageOut =
            item.imageOut || "";


        let imageText = "--";


        if (
            imageIn ||
            imageOut
        ) {

            imageText =
                "📷 Có ảnh";

        }


        html += `

            <tr>

                <td>

                    <strong>
                        ${escapeHtml(
                            item.plate || "---"
                        )}
                    </strong>

                </td>


                <td>
                    ${escapeHtml(
                        item.slot || "---"
                    )}
                </td>


                <td>
                    ${escapeHtml(
                        formatTime(
                            item.timeIn
                        )
                    )}
                </td>


                <td>
                    ${escapeHtml(
                        formatTime(
                            item.timeOut
                        )
                    )}
                </td>


                <td>
                    ${imageText}
                </td>

            </tr>

        `;

    });


    if (filtered.length === 0) {

        html = `

            <tr>

                <td
                    colspan="5"
                    class="empty-message"
                >

                    Không tìm thấy dữ liệu

                </td>

            </tr>

        `;

    }


    table.innerHTML = html;

}


/* =====================================
   THỐNG KÊ
===================================== */

function updateHistoryStatistics() {

    const totalTrips =
        document.getElementById(
            "totalTrips"
        );


    const averageTime =
        document.getElementById(
            "averageTime"
        );


    /*
     * Tổng số lượt xe
     */

    if (totalTrips) {

        totalTrips.innerText =
            allHistory.length;

    }


    /*
     * Tính thời gian đỗ trung bình
     *
     * Chỉ tính những xe đã ra.
     */

    let totalMinutes = 0;

    let completedTrips = 0;


    allHistory.forEach(item => {

        if (
            item.timeIn &&
            item.timeOut
        ) {

            const start =
                parseVietnameseDate(
                    item.timeIn
                );


            const end =
                parseVietnameseDate(
                    item.timeOut
                );


            if (
                start &&
                end &&
                end > start
            ) {

                const minutes =
                    (
                        end - start
                    ) / 60000;


                totalMinutes +=
                    minutes;


                completedTrips++;

            }

        }

    });


    if (
        averageTime &&
        completedTrips > 0
    ) {

        const avg =
            totalMinutes /
            completedTrips;


        const hours =
            Math.floor(
                avg / 60
            );


        const minutes =
            Math.round(
                avg % 60
            );


        if (hours > 0) {

            averageTime.innerText =
                `${hours}h ${minutes}p`;

        }

        else {

            averageTime.innerText =
                `${minutes} phút`;

        }

    }

    else if (averageTime) {

        averageTime.innerText =
            "--";

    }

}


/* =====================================
   TÌM KIẾM BIỂN SỐ
===================================== */

const searchPlate =
    document.getElementById(
        "searchPlate"
    );


if (searchPlate) {

    searchPlate.addEventListener(
        "input",
        () => {

            updateHistoryTable(
                searchPlate.value
            );

        }
    );

}


/* =====================================
   XE VÀO
===================================== */

window.carIn = async function(slot) {

    try {

        /*
         * Kiểm tra vị trí
         */

        const slotData =
            currentSlots[slot];


        if (
            slotData &&
            slotData.occupied === true
        ) {

            alert(
                `${slot} đang có xe!`
            );

            return;

        }


        /*
         * Nhập biển số tạm thời.
         *
         * Sau này sẽ thay bằng
         * ESP32-CAM + nhận diện biển số.
         */

        const plate =
            prompt(
                `Nhập biển số xe vào ${slot}:`
            );


        if (plate === null) {
            return;
        }


        const cleanPlate =
            plate.trim();


        if (!cleanPlate) {

            alert(
                "Vui lòng nhập biển số!"
            );

            return;

        }


        /*
         * Thời gian
         */

        const now =
            new Date();


        const timeIn =
            now.toLocaleString(
                "vi-VN"
            );


        /*
         * Tạo ID history
         */

        const newHistoryRef =
            push(
                ref(
                    db,
                    "history"
                )
            );


        const historyId =
            newHistoryRef.key;


        /*
         * Dữ liệu history
         */

        const historyItem = {

            plate:
                cleanPlate,

            slot:
                slot,

            timeIn:
                timeIn,

            timeOut:
                "",

            imageIn:
                "",

            imageOut:
                ""

        };


        /*
         * Ghi nhiều vị trí
         * trong một lần update.
         */

        const updates = {};


        updates[
            `slots/${slot}/occupied`
        ] = true;


        updates[
            `slots/${slot}/plate`
        ] = cleanPlate;


        updates[
            `slots/${slot}/historyId`
        ] = historyId;


        updates[
            `history/${historyId}`
        ] = historyItem;


        await update(
            ref(db),
            updates
        );


        alert(
            `Xe ${cleanPlate} đã vào ${slot}`
        );

    }

    catch (error) {

        console.error(
            "Lỗi xe vào:",
            error
        );


        alert(
            "Không thể lưu dữ liệu!"
        );

    }

};


/* =====================================
   XE RA
===================================== */

window.carOut = async function(slot) {

    try {

        const slotData =
            currentSlots[slot];


        /*
         * Không có xe
         */

        if (
            !slotData ||
            slotData.occupied !== true
        ) {

            alert(
                `${slot} đang trống!`
            );

            return;

        }


        const plate =
            slotData.plate || "---";


        /*
         * historyId được lấy
         * trực tiếp từ Firebase
         *
         * nên F5 cũng không mất.
         */

        const historyId =
            slotData.historyId;


        /*
         * Thời gian xe ra
         */

        const now =
            new Date();


        const timeOut =
            now.toLocaleString(
                "vi-VN"
            );


        const updates = {};


        /*
         * Trả vị trí về trống
         */

        updates[
            `slots/${slot}/occupied`
        ] = false;


        updates[
            `slots/${slot}/plate`
        ] = "";


        updates[
            `slots/${slot}/historyId`
        ] = "";


        /*
         * Cập nhật lịch sử
         */

        if (historyId) {

            updates[
                `history/${historyId}/timeOut`
            ] = timeOut;

        }


        await update(
            ref(db),
            updates
        );


        alert(
            `Xe ${plate} đã ra khỏi ${slot}`
        );

    }

    catch (error) {

        console.error(
            "Lỗi xe ra:",
            error
        );


        alert(
            "Không thể cập nhật dữ liệu!"
        );

    }

};


/* =====================================
   BẢO VỆ HTML
===================================== */

function escapeHtml(value) {

    return String(value)

        .replaceAll("&", "&amp;")

        .replaceAll("<", "&lt;")

        .replaceAll(">", "&gt;")

        .replaceAll('"', "&quot;")

        .replaceAll("'", "&#039;");

}


/* =====================================
   ĐỌC NGÀY VIỆT NAM
===================================== */

function parseVietnameseDate(value) {

    if (!value) {
        return null;
    }


    /*
     * Dạng:
     *
     * 23/09/2026, 08:30:00
     *
     * hoặc tùy trình duyệt:
     * 23/09/2026 08:30:00
     */

    const match =
        String(value).match(
            /(\d{1,2})\/(\d{1,2})\/(\d{4})[,\s]+(\d{1,2}):(\d{2})(?::(\d{2}))?/
        );


    if (!match) {

        return new Date(value);

    }


    const day =
        Number(match[1]);


    const month =
        Number(match[2]) - 1;


    const year =
        Number(match[3]);


    const hour =
        Number(match[4]);


    const minute =
        Number(match[5]);


    const second =
        Number(match[6] || 0);


    return new Date(
        year,
        month,
        day,
        hour,
        minute,
        second
    );

}