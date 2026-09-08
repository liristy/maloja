async function upload(encodedentity,b64) {
	try {
		const response = await fetch("/apis/mlj_1/addpicture?" + encodedentity, {
			method: "POST",
			body: new URLSearchParams({b64: b64})
		});
		const result = await response.json();
		if (!response.ok || result.status !== "success") throw new Error("Upload failed");
		window.location.reload();
	} catch (error) {
		window.alert("Cover could not be saved. Please try again.");
	}
}
